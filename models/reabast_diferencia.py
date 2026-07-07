from markupsafe import Markup
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare
from odoo.tools.misc import html_escape


class ReabastDiferencia(models.Model):
    """Reporte de diferencias de una recepción de reabastecimiento (sub-ladrillo 5).

    Cabecera atada 1-a-1 al picking de recepción. La sucursal la genera al "Informar diferencias"
    (5a); Central la resuelve línea por línea desde su bandeja (5b). Vive dentro del módulo (C.2):
    no se agregan campos a stock.picking salvo el m2o de vuelta (visibilidad del botón/estado).
    """
    _name = 'yaguven.reabast.diferencia'
    _description = 'Diferencia de recepción de reabastecimiento'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(string='Referencia', readonly=True, copy=False, index=True)
    picking_id = fields.Many2one(
        'stock.picking', string='Recepción', required=True, readonly=True, ondelete='cascade',
        index=True, help='Recepción de reabastecimiento sobre la que se informó la diferencia.')
    sucursal_id = fields.Many2one(
        'stock.warehouse', string='Sucursal', readonly=True,
        help='Sucursal que recibió la mercadería e informó la diferencia.')
    company_id = fields.Many2one(related='picking_id.company_id', store=True, readonly=True)

    state = fields.Selection(
        [('pendiente', 'Pendiente'),
         ('en_resolucion', 'En resolución'),
         ('resuelta', 'Resuelta')],
        string='Estado', default='pendiente', tracking=True, readonly=True, copy=False,
        help='Pendiente (informada, Central todavía no la tocó) → En resolución (Central resolvió '
             'algunas líneas) → Resuelta (todas las líneas resueltas).')

    line_ids = fields.One2many(
        'yaguven.reabast.diferencia.linea', 'diferencia_id', string='Diferencias')

    informado_por_id = fields.Many2one(
        'res.users', string='Informó', readonly=True, default=lambda self: self.env.user)
    fecha_informe = fields.Datetime(
        string='Informada el', readonly=True, default=fields.Datetime.now)

    pendientes_count = fields.Integer(
        string='Líneas pendientes', compute='_compute_pendientes_count',
        help='Cantidad de líneas de la diferencia todavía sin resolver.')

    _picking_uniq = models.Constraint(
        'unique(picking_id)',
        'Ya existe un reporte de diferencias para esta recepción.',
    )

    @api.depends('line_ids.resuelta')
    def _compute_pendientes_count(self):
        for dif in self:
            dif.pendientes_count = len(dif.line_ids.filtered(lambda l: not l.resuelta))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name'):
                picking = self.env['stock.picking'].browse(vals.get('picking_id'))
                # Nombre derivado del picking (1 diferencia por recepción): estable, legible y único
                # sin depender de una ir.sequence. Ej. recepción "WH/RREP/00018" -> "DIF/WH/RREP/00018".
                vals['name'] = 'DIF/%s' % (picking.name or _('Nueva'))
        return super().create(vals_list)

    def _sincronizar_estado(self):
        """Recalcula el estado de la cabecera según las líneas resueltas. Lo llama 5b al resolver."""
        for dif in self:
            lineas = dif.line_ids
            if lineas and all(l.resuelta for l in lineas):
                dif.state = 'resuelta'
            elif any(l.resuelta for l in lineas):
                dif.state = 'en_resolucion'
            else:
                dif.state = 'pendiente'

    # ------------------------------------------------------------------
    # 5b — Resolución en Central
    # ------------------------------------------------------------------
    def action_aplicar_resoluciones(self):
        """Ejecuta las resoluciones que Central cargó en las líneas pendientes.

        Procesa SOLO las líneas con una resolución elegida y todavía sin resolver (idempotente:
        re-aplicar no duplica movimientos, B.7). Cada tipo de resolución genera lo que corresponde
        (pedido de reenvío, devolución, ambos, o nada) y marca la línea `resuelta`.
        """
        self.ensure_one()
        handlers = {
            'autorizar_faltante': self._resolver_autorizar_faltante,
            'devolucion': self._resolver_devolucion,
            'reemplazo': self._resolver_reemplazo,
            'ajuste': self._resolver_ajuste,
        }
        pendientes = self.line_ids.filtered(lambda l: l.resolucion and not l.resuelta)
        if not pendientes:
            raise UserError(_(
                "No hay líneas con una resolución para aplicar. Elegí una resolución en las "
                "líneas pendientes y volvé a intentar."))
        for linea in pendientes:
            handlers[linea.resolucion](linea)
        self._sincronizar_estado()
        return True

    def _resolver_ajuste(self, linea):
        """Asume la diferencia. La recepción se validó con lo físico real (5a, sin backorder), así
        que el stock ya refleja la realidad: no hay movimiento que generar, solo se cierra la línea."""
        linea.resuelta = True
        self._post_resolucion(linea, _(
            "Ajuste de transferencia: se asume la diferencia. El stock ya refleja lo recibido "
            "físicamente; no se genera movimiento."))

    def _resolver_autorizar_faltante(self, linea):
        """Genera un pedido de reabastecimiento (borrador) por el faltante, para reenviarlo por el
        circuito normal (pedido → armar → despacho → recepción)."""
        producto = linea.producto_esperado_id
        falta = linea.cant_esperada - linea.cant_recibida
        if not producto or float_compare(falta, 0.0, precision_rounding=producto.uom_id.rounding) <= 0:
            raise UserError(_(
                "La línea «%s» no tiene un faltante para autorizar (recibido ≥ esperado).",
                (producto or linea.producto_recibido_id).display_name))
        pedido = self._generar_pedido_faltante(producto, falta)
        linea.pedido_generado_id = pedido.id
        linea.resuelta = True
        self._post_resolucion(linea, _(
            "Autorizado el reenvío del faltante: %(cant)s × %(prod)s. Se generó el pedido %(ped)s "
            "(borrador) para despacharlo por el circuito.",
            cant=('%g' % falta), prod=producto.display_name, ped=pedido.name))

    def _resolver_devolucion(self, linea):
        """Crea un traslado de devolución sucursal → central por lo que llegó de más o equivocado."""
        producto, cant = self._devolucion_producto_cant(linea)
        picking = self._generar_devolucion(producto, cant)
        linea.move_generado_id = picking.move_ids[:1].id
        linea.resuelta = True
        self._post_resolucion(linea, _(
            "Pedida la devolución: %(cant)s × %(prod)s vuelven a Central. Se generó el traslado "
            "%(pick)s.", cant=('%g' % cant), prod=producto.display_name, pick=picking.name))

    def _resolver_reemplazo(self, linea):
        """Producto incorrecto: devuelve lo que vino mal Y autoriza el envío del correcto."""
        producto_mal, cant_mal = self._devolucion_producto_cant(linea)
        picking = self._generar_devolucion(producto_mal, cant_mal)
        correcto = linea.producto_esperado_id
        if not correcto or float_compare(linea.cant_esperada, 0.0,
                                         precision_rounding=(correcto.uom_id.rounding if correcto else 0.01)) <= 0:
            raise UserError(_(
                "El reemplazo necesita un producto esperado con cantidad para reenviar (línea de %s).",
                producto_mal.display_name))
        pedido = self._generar_pedido_faltante(correcto, linea.cant_esperada)
        linea.move_generado_id = picking.move_ids[:1].id
        linea.pedido_generado_id = pedido.id
        linea.resuelta = True
        self._post_resolucion(linea, _(
            "Reemplazo aprobado: devolución de %(cm)s × %(pm)s (traslado %(pick)s) y reenvío de "
            "%(cc)s × %(pc)s (pedido %(ped)s).",
            cm=('%g' % cant_mal), pm=producto_mal.display_name, pick=picking.name,
            cc=('%g' % linea.cant_esperada), pc=correcto.display_name, ped=pedido.name))

    # --- helpers de generación ---
    def _devolucion_producto_cant(self, linea):
        """Qué producto y cuánto vuelve a Central según el tipo de diferencia."""
        if linea.tipo == 'incorrecto':
            # Vino otro producto: vuelve todo lo recibido equivocado.
            producto = linea.producto_recibido_id
            cant = linea.cant_recibida
        else:
            # Sobrante / cantidad de más: vuelve el excedente del mismo producto.
            producto = linea.producto_recibido_id or linea.producto_esperado_id
            cant = linea.cant_recibida - linea.cant_esperada
        if not producto or float_compare(
                cant, 0.0, precision_rounding=producto.uom_id.rounding) <= 0:
            raise UserError(_(
                "La línea «%s» no tiene un excedente para devolver.",
                (producto or linea.producto_esperado_id).display_name))
        return producto, cant

    def _generar_pedido_faltante(self, producto, cant):
        """Crea un pedido de reabastecimiento en borrador para la sucursal de esta diferencia."""
        return self.env['yaguven.reabast.pedido'].create({
            'sucursal_id': self.sucursal_id.id,
            'note': _("Generado al resolver la diferencia %(dif)s de la recepción %(rec)s.",
                      dif=self.name, rec=self.picking_id.name),
            'line_ids': [(0, 0, {'product_id': producto.id, 'product_uom_qty': cant})],
        })

    def _generar_devolucion(self, producto, cant):
        """Traslado interno de devolución sucursal → central (paso 'devolucion'), en borrador
        confirmado para que Central/Depósito lo valide cuando la mercadería vuelve físicamente."""
        tipo = self.env['stock.picking.type'].search(
            [('yaguven_reabast_paso', '=', 'devolucion')], limit=1)
        if not tipo:
            raise UserError(_(
                "No hay un tipo de operación de Devolución configurado (paso 'devolucion'). "
                "Configuralo antes de resolver por devolución o reemplazo."))
        src = self.sucursal_id.lot_stock_id            # Existencias de la sucursal que devuelve
        dest = tipo.default_location_dest_id           # Existencias de Central
        picking = self.env['stock.picking'].create({
            'picking_type_id': tipo.id,
            'location_id': src.id,
            'location_dest_id': dest.id,
            'origin': self.name,
            'move_ids': [(0, 0, {
                'name': producto.display_name,
                'product_id': producto.id,
                'product_uom_qty': cant,
                'product_uom': producto.uom_id.id,
                'location_id': src.id,
                'location_dest_id': dest.id,
            })],
        })
        picking.action_confirm()
        return picking

    def _post_resolucion(self, linea, texto):
        """Nota en el chatter de la diferencia (C.4: Markup + mt_note + html_escape)."""
        self.message_post(
            body=Markup("<p>%s</p>") % html_escape(texto),
            message_type="comment", subtype_xmlid="mail.mt_note")


class ReabastDiferenciaLinea(models.Model):
    _name = 'yaguven.reabast.diferencia.linea'
    _description = 'Línea de diferencia de recepción'
    _order = 'diferencia_id, id'

    diferencia_id = fields.Many2one(
        'yaguven.reabast.diferencia', string='Reporte de diferencia', required=True,
        ondelete='cascade', index=True)
    sucursal_id = fields.Many2one(related='diferencia_id.sucursal_id', store=True, readonly=True)

    tipo = fields.Selection(
        [('faltante', 'Faltante'),
         ('sobrante', 'Sobrante'),
         ('incorrecto', 'Producto incorrecto'),
         ('cantidad', 'Cantidad distinta')],
        string='Tipo', required=True,
        help='Faltante: llegó menos que lo despachado. Sobrante: llegó de más. Producto '
             'incorrecto: vino otro producto (color/medida/presentación). Cantidad distinta: '
             'la cantidad recibida no coincide con la esperada.')

    producto_esperado_id = fields.Many2one(
        'product.product', string='Producto esperado',
        help='Lo que decía el remito/transferencia. Vacío en un sobrante puro (nada esperado).')
    producto_recibido_id = fields.Many2one(
        'product.product', string='Producto recibido',
        help='Lo que realmente llegó. Se usa en sobrante (producto de más) y en producto '
             'incorrecto (lo que vino en lugar de lo esperado).')

    cant_esperada = fields.Float(string='Cant. esperada')
    cant_recibida = fields.Float(string='Cant. recibida')
    cant_diferencia = fields.Float(
        string='Diferencia', compute='_compute_cant_diferencia', store=True,
        help='Recibida − esperada. Negativa = faltó; positiva = sobró.')

    nota = fields.Char(string='Nota', help='Aclaración libre de la sucursal (ej. "vino grano 120 en vez de 100").')

    # --- Resolución (la completa Central en 5b) ---
    resolucion = fields.Selection(
        [('autorizar_faltante', 'Autorizar envío del faltante'),
         ('devolucion', 'Pedir devolución'),
         ('reemplazo', 'Aprobar reemplazo'),
         ('ajuste', 'Ajustar transferencia')],
        string='Resolución', copy=False,
        help='Decisión de Central para regularizar esta diferencia. La elige Central en la '
             'bandeja de diferencias y se ejecuta con "Aplicar resoluciones".')
    resuelta = fields.Boolean(string='Resuelta', default=False, readonly=True, copy=False)
    move_generado_id = fields.Many2one(
        'stock.move', string='Movimiento generado', readonly=True, copy=False,
        help='Movimiento de stock que generó la resolución (envío de faltante, devolución, etc.).')
    pedido_generado_id = fields.Many2one(
        'yaguven.reabast.pedido', string='Pedido generado', readonly=True, copy=False,
        help='Pedido de reabastecimiento que generó la resolución (ej. backorder del faltante).')

    @api.depends('cant_esperada', 'cant_recibida')
    def _compute_cant_diferencia(self):
        for ln in self:
            ln.cant_diferencia = ln.cant_recibida - ln.cant_esperada
