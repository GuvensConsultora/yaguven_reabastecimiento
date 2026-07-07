from odoo import api, fields, models, _
from odoo.tools import float_compare


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

    _sql_constraints = [
        ('picking_uniq', 'unique(picking_id)',
         'Ya existe un reporte de diferencias para esta recepción.'),
    ]

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


class ReabastDiferenciaLinea(models.Model):
    _name = 'yaguven.reabast.diferencia.linea'
    _description = 'Línea de diferencia de recepción'
    _order = 'diferencia_id, id'

    diferencia_id = fields.Many2one(
        'yaguven.reabast.diferencia', string='Diferencia', required=True, ondelete='cascade',
        index=True)
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
        string='Resolución', readonly=True, copy=False,
        help='Decisión de Central para regularizar esta diferencia.')
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
