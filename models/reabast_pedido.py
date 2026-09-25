from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ReabastPedido(models.Model):
    _name = 'yaguven.reabast.pedido'
    _description = 'Pedido de reabastecimiento'
    _inherit = ['mail.thread']
    _order = 'fecha desc, id desc'

    name = fields.Char(string='Número', default='Nuevo', copy=False, readonly=True, index=True)
    sucursal_id = fields.Many2one(
        'stock.warehouse', string='Sucursal', required=True, tracking=True,
        domain="[('operating_unit_id', '!=', False)]",
        help='Almacén/sucursal que pide el reabastecimiento.')
    # related store=True -> habilita el scoping por Unidad Operativa (regla de registro) y "Armar"
    operating_unit_id = fields.Many2one(
        'operating.unit', string='Unidad Operativa',
        related='sucursal_id.operating_unit_id', store=True, index=True)
    fecha = fields.Date(string='Fecha', default=fields.Date.context_today, tracking=True)
    user_id = fields.Many2one(
        'res.users', string='Responsable', default=lambda self: self.env.user, tracking=True)
    state = fields.Selection(
        [('borrador', 'Borrador'),
         ('enviado', 'Enviado'),
         ('procesado', 'Procesado'),
         ('cancelado', 'Cancelado')],
        string='Estado', default='borrador', required=True, tracking=True,
        help='Borrador (editable) → Enviado (lo toma "Armar recolección") → Procesado / Cancelado.')
    line_ids = fields.One2many('yaguven.reabast.pedido.line', 'pedido_id', string='Líneas')
    # de dónde salió el pedido: cargado a mano; generado desde las reglas de mín/máx para que la
    # sucursal cuente (botón «Pedir mercadería»); o armado solo por el proceso diario, en borrador
    # para que Central lo revise (acordado con Anael 23/09: la sucursal no cuenta ni maneja mín/máx).
    origen = fields.Selection(
        [('manual', 'Manual'), ('minmax', 'Mín/máx'), ('auto', 'Automático')],
        string='Origen', default='manual', required=True, readonly=True, copy=False,
        tracking=True)
    faltan_contar = fields.Integer(
        string='Faltan contar', compute='_compute_faltan_contar',
        help='Productos de mín/máx que la sucursal todavía no contó.')
    note = fields.Text(string='Observaciones')
    # vínculo a la recolección consolidada que procesó el pedido (campo en NUESTRO modelo, no en
    # el nativo stock.picking — C.2). Lo setea action_armar_recoleccion (lo invoca el wizard 3b).
    picking_recoleccion_id = fields.Many2one(
        'stock.picking', string='Recolección', readonly=True, copy=False, tracking=True,
        help='Recolección consolidada en la que se procesó este pedido.')
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True,
        default=lambda self: self.env.company)

    @api.depends('origen', 'line_ids.contado', 'line_ids.orderpoint_id')
    def _compute_faltan_contar(self):
        for pedido in self:
            pedido.faltan_contar = len(pedido.line_ids.filtered(
                lambda l: l.orderpoint_id and not l.contado)) if pedido.origen == 'minmax' else 0

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'Nuevo') in (False, 'Nuevo'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'yaguven.reabast.pedido') or 'Nuevo'
        return super().create(vals_list)

    def action_enviar(self):
        for pedido in self:
            if pedido.state != 'borrador':
                raise UserError(_("Solo se puede enviar un pedido en borrador."))
            if not pedido.line_ids:
                raise UserError(_("El pedido no tiene líneas: agregá al menos un producto."))
            sin_contar = pedido.line_ids.filtered(lambda l: l.orderpoint_id and not l.contado) \
                if pedido.origen == 'minmax' else pedido.line_ids.browse()
            if sin_contar:
                raise UserError(_(
                    "Faltan contar %s productos. Anotá cuántos hay en el local "
                    "(si no hay ninguno, poné 0).") % len(sin_contar))
            pedido.state = 'enviado'
        return True

    def action_borrador(self):
        for pedido in self:
            if pedido.state == 'procesado':
                raise UserError(_("Un pedido ya procesado no vuelve a borrador."))
            pedido.state = 'borrador'
        return True

    def action_cancelar(self):
        for pedido in self:
            if pedido.state == 'procesado':
                raise UserError(_("Un pedido ya procesado no se puede cancelar."))
            pedido.state = 'cancelado'
        return True

    # ------------------------------------------------------------------
    # Pedir mercadería por mín/máx — el mín/máx entra al circuito como un pedido más
    # ------------------------------------------------------------------
    @api.model
    def _tope_minmax(self):
        """Cuántos productos entran como máximo en un pedido de mín/máx: lo que una persona de
        sucursal puede contar de una vez. Configurable (Inventario › Configuración), no fijo acá."""
        valor = self.env['ir.config_parameter'].sudo().get_int('yaguven_reabastecimiento.tope_minmax', 0)
        return valor if valor > 0 else 0

    @api.model
    def _candidatos_minmax(self, sucursal):
        """Lo que le falta a una sucursal según sus reglas que se surten desde Central, más
        urgente primero. Lo usan los dos caminos: el pedido para contar y el automático.

        - Solo reglas cuya ruta surte desde Existencias de Central (las de «Comprar» quedan afuera).
        - Se descuenta lo que la sucursal ya tiene en pedidos en borrador/enviados (Odoo no los
          ve todavía como movimientos) para no pedir dos veces.
        Devuelve ([(urgencia, regla)], {producto: cantidad ya pedida})."""
        loc_central = self._tipo_recoleccion().default_location_src_id
        ya_pedido = defaultdict(float)
        for ln in self.search([
                ('sucursal_id', '=', sucursal.id),
                ('state', 'in', ('borrador', 'enviado'))]).line_ids:
            ya_pedido[ln.product_id] += ln.product_uom_qty
        candidatos = []
        for op in self.env['stock.warehouse.orderpoint'].search([
                ('warehouse_id', '=', sucursal.id),
                ('route_id.rule_ids.location_src_id', '=', loc_central.id)]):
            if op.qty_to_order <= 0 or ya_pedido[op.product_id] >= op.qty_to_order:
                continue
            # urgencia: cuánto le falta para el mínimo, relativo al propio mínimo
            urgencia = (op.product_min_qty - op.qty_forecast) / max(op.product_min_qty, 1)
            candidatos.append((urgencia, op))
        candidatos.sort(key=lambda c: c[0], reverse=True)
        return candidatos, ya_pedido

    @api.model
    def _linea_desde_regla(self, op, ya_pedido, seq):
        en_camino = max(op.qty_forecast - op.qty_on_hand, 0.0) + ya_pedido[op.product_id]
        return (0, 0, {
            'sequence': seq,
            'product_id': op.product_id.id,
            'orderpoint_id': op.id,
            'stock_sistema': op.qty_on_hand,
            'en_camino': en_camino,
            'min_qty': op.product_min_qty,
            'max_qty': op.product_max_qty,
            # con el stock del sistema; en el pedido para contar se recalcula al contar
            'product_uom_qty': max(op.product_max_qty - op.qty_on_hand - en_camino, 0.0),
        })

    @api.model
    def _cron_pedidos_automaticos(self):
        """Proceso diario: por cada sucursal con circuito de reabastecimiento, deja en BORRADOR
        un pedido «Automático» con lo que está bajo el mínimo, para que Central lo revise y lo
        envíe. Sin conteo y sin tope: la revisión es de Central.

        Si la sucursal ya tiene un borrador automático sin enviar, sólo se le AGREGAN los productos
        que no tiene: las cantidades que Central ya corrigió no se pisan."""
        recep = self.env['stock.picking.type'].search([('yaguven_reabast_paso', '=', 'recepcion')])
        creados = actualizados = self.browse()
        for sucursal in recep.warehouse_id:
            candidatos, ya_pedido = self._candidatos_minmax(sucursal)
            existente = self.search([
                ('sucursal_id', '=', sucursal.id),
                ('origen', '=', 'auto'), ('state', '=', 'borrador')], limit=1)
            if existente:
                ya_esta = existente.line_ids.product_id
                candidatos = [c for c in candidatos if c[1].product_id not in ya_esta]
            if not candidatos:
                continue
            candidatos.sort(key=lambda c: (c[1].product_id.default_code or '',
                                           c[1].product_id.name or ''))
            base = max(existente.line_ids.mapped('sequence') or [0])
            lineas = [self._linea_desde_regla(op, ya_pedido, base + i)
                      for i, (_u, op) in enumerate(candidatos, 1)]
            if existente:
                existente.line_ids = lineas
                existente.message_post(body=_(
                    "El proceso automático agregó %s productos bajo el mínimo.") % len(lineas))
                actualizados |= existente
            else:
                creados |= self.create({
                    'sucursal_id': sucursal.id, 'origen': 'auto',
                    'company_id': sucursal.company_id.id, 'line_ids': lineas,
                })
        return creados, actualizados

    @api.model
    def _traer_minmax(self, sucursales):
        """Por cada sucursal, arma UN pedido en borrador de origen mín/máx con los productos que
        el sistema ve bajo mínimo, para que la sucursal cuente cuántos hay de verdad en el local.

        - Solo reglas cuya ruta surte desde Existencias de Central (las de «Comprar» quedan afuera).
        - Entran hasta el tope configurado, los más lejos del mínimo primero; el resto sale en el
          próximo pedido.
        - Se descuenta lo que la sucursal ya tiene en otros pedidos en borrador/enviados (Odoo no
          los ve todavía como movimientos) para no pedir dos veces.
        - Un borrador de mín/máx que ya existe NO se toca: puede tener conteos cargados.
        Devuelve (pedidos_nuevos, pedidos_existentes)."""
        tope = self._tope_minmax()
        nuevos = existentes = self.browse()
        for sucursal in sucursales:
            existente = self.search([
                ('sucursal_id', '=', sucursal.id),
                ('origen', '=', 'minmax'), ('state', '=', 'borrador')], limit=1)
            if existente:
                existentes |= existente
                continue

            candidatos, ya_pedido = self._candidatos_minmax(sucursal)
            if tope:
                candidatos = candidatos[:tope]
            if not candidatos:
                continue

            # se eligen por urgencia, pero se muestran por código: el código agrupa por familia
            # (07.02 látex, 08.04 Plavicon…) y así se cuenta góndola por góndola. La categoría no
            # sirve: en Lupatini todos los productos están en «Todos» (medido 23/09, O17 y O19).
            candidatos.sort(key=lambda c: (c[1].product_id.default_code or '',
                                           c[1].product_id.name or ''))
            lineas = [self._linea_desde_regla(op, ya_pedido, seq)
                      for seq, (_urg, op) in enumerate(candidatos, 1)]
            nuevos |= self.create({
                'sucursal_id': sucursal.id, 'origen': 'minmax',
                'company_id': sucursal.company_id.id, 'line_ids': lineas,
            })
        return nuevos, existentes

    @api.model
    def _sucursales_del_usuario(self):
        """Sucursales que el usuario puede pedir: las de sus Unidades Operativas que tienen
        circuito de reabastecimiento (tipo de Recepción REAB). Así Central y los móviles quedan
        afuera sin nombrarlos."""
        recep = self.env['stock.picking.type'].search([('yaguven_reabast_paso', '=', 'recepcion')])
        sucursales = recep.warehouse_id
        if not self.env.user.has_group('yaguven_reabastecimiento.group_reabast_supervisor'):
            sucursales = sucursales.filtered(
                lambda w: w.operating_unit_id in self.env.user.operating_unit_ids)
        return sucursales

    @api.model
    def action_pedir_mercaderia(self):
        """Menú «Pedir mercadería». Una sucursal → abre directo su pedido para contar.
        Varias (Central) → ventana para elegir cuáles (ver _accion_elegir_sucursales)."""
        sucursales = self._sucursales_del_usuario()
        if not sucursales:
            raise UserError(_(
                "Tu usuario no tiene una sucursal asignada para pedir mercadería. "
                "Pedile a Central que te la asigne."))
        if len(sucursales) > 1:
            return self._accion_elegir_sucursales(sucursales)
        nuevos, existentes = self._traer_minmax(sucursales)
        pedido = nuevos or existentes
        if not pedido:
            return {
                'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'info', 'sticky': False,
                           'title': _('No hace falta pedir nada'),
                           'message': _('Según el sistema, a tu sucursal no le falta nada hoy.')},
            }
        return pedido._accion_abrir_conteo()

    def _accion_abrir_conteo(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pedir mercadería'),
            'res_model': 'yaguven.reabast.pedido',
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(self.env.ref('yaguven_reabastecimiento.view_reabast_pedido_form_conteo').id,
                       'form')],
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Armar recolección (Etapa 3b) — motor invocado por el wizard de confirmación
    # ------------------------------------------------------------------
    def _tipo_recoleccion(self):
        """Tipo de picking de Recolección de Central (único). Resuelto por paso, sin ids fijos."""
        tipo = self.env['stock.picking.type'].search(
            [('yaguven_reabast_paso', '=', 'recoleccion')], limit=1)
        if not tipo:
            raise UserError(_(
                "No está configurado el tipo de operación 'Recolección' de reabastecimiento. "
                "Revisá la topología de reabastecimiento de Central."))
        return tipo

    def _tipo_despacho_sucursal(self, sucursal):
        """Resuelve los tipos del tramo a una sucursal: vía su tipo de Recepción (que cuelga del
        almacén-sucursal) obtenemos el tránsito, y de ahí el Despacho. Devuelve
        (despacho, transito, recepción) para encadenar los tres pasos del circuito."""
        Tipo = self.env['stock.picking.type']
        recep = Tipo.search([
            ('yaguven_reabast_paso', '=', 'recepcion'),
            ('warehouse_id', '=', sucursal.id)], limit=1)
        if not recep:
            raise UserError(_(
                "La sucursal «%s» no tiene topología de reabastecimiento (falta el tipo de "
                "Recepción). Configurala antes de armar la recolección.") % sucursal.display_name)
        transito = recep.default_location_src_id
        desp = Tipo.search([
            ('yaguven_reabast_paso', '=', 'despacho'),
            ('default_location_dest_id', '=', transito.id)], limit=1)
        if not desp:
            raise UserError(_(
                "La sucursal «%s» no tiene tipo de Despacho hacia su tránsito.") % sucursal.display_name)
        return desp, transito, recep

    def action_armar_recoleccion(self):
        """Consolida los pedidos 'enviado' de self en UNA recolección (un move por producto,
        cantidad total) + un despacho por sucursal (encadenado por move_orig_ids). Marca los
        pedidos como 'procesado' y los vincula a la recolección. Mecanismo verificado en vivo (3b).
        Lo invoca el wizard de confirmación tras la pantalla previa."""
        pedidos = self.filtered(lambda p: p.state == 'enviado')
        if not pedidos:
            raise UserError(_("No hay pedidos enviados para armar la recolección."))

        reco_type = self._tipo_recoleccion()
        loc_exist = reco_type.default_location_src_id
        loc_salida = reco_type.default_location_dest_id

        # la compañía la fija el tipo de operación de Central (no la del usuario, que puede ser otra
        # en multicompañía) -> evita el cruce de empresas en pickings/moves (C.1, company_dependent)
        comp = reco_type.company_id
        Picking = self.env['stock.picking'].with_company(comp)
        Move = self.env['stock.move'].with_company(comp)

        # acumular cantidades: total por producto (recolección) y por sucursal+producto (despachos)
        total_prod = defaultdict(float)
        suc_prod = defaultdict(lambda: defaultdict(float))
        for ped in pedidos:
            for ln in ped.line_ids.filtered(lambda l: l.product_uom_qty > 0):
                total_prod[ln.product_id] += ln.product_uom_qty
                suc_prod[ped.sucursal_id][ln.product_id] += ln.product_uom_qty

        # 1) recolección consolidada: un move por producto (cantidad total)
        reco_pick = Picking.create({
            'picking_type_id': reco_type.id, 'company_id': comp.id,
            'location_id': loc_exist.id, 'location_dest_id': loc_salida.id,
            'origin': _('Reabastecimiento'),
        })
        reco_moves = {}
        for prod, qty in total_prod.items():
            reco_moves[prod] = Move.create({
                'product_id': prod.id, 'product_uom_qty': qty, 'company_id': comp.id,
                'location_id': loc_exist.id, 'location_dest_id': loc_salida.id,
                'picking_id': reco_pick.id, 'picking_type_id': reco_type.id,
            })

        # 2) por sucursal: un despacho (Salida→Tránsito) encadenado a la recolección, y una
        #    recepción (Tránsito→Existencias sucursal) encadenada al despacho. Los tres tramos
        #    quedan ligados por move_orig_ids/make_to_order -> el gateo recolección→despacho→
        #    recepción funciona y el stock llega efectivamente a la sucursal.
        pickings = reco_pick
        for sucursal, prods in suc_prod.items():
            desp_type, transito, recep_type = self._tipo_despacho_sucursal(sucursal)
            loc_suc = recep_type.default_location_dest_id   # Existencias de la sucursal
            desp_pick = Picking.create({
                'picking_type_id': desp_type.id, 'company_id': comp.id,
                'location_id': loc_salida.id, 'location_dest_id': transito.id,
                'origin': _('Reabastecimiento → %s') % sucursal.display_name,
            })
            recep_pick = Picking.create({
                'picking_type_id': recep_type.id, 'company_id': comp.id,
                'location_id': transito.id, 'location_dest_id': loc_suc.id,
                'origin': _('Reabastecimiento → %s') % sucursal.display_name,
            })
            for prod, qty in prods.items():
                desp_move = Move.create({
                    'product_id': prod.id, 'product_uom_qty': qty, 'company_id': comp.id,
                    'location_id': loc_salida.id, 'location_dest_id': transito.id,
                    'picking_id': desp_pick.id, 'picking_type_id': desp_type.id,
                    'procure_method': 'make_to_order',
                    'move_orig_ids': [(4, reco_moves[prod].id)],
                })
                Move.create({
                    'product_id': prod.id, 'product_uom_qty': qty, 'company_id': comp.id,
                    'location_id': transito.id, 'location_dest_id': loc_suc.id,
                    'picking_id': recep_pick.id, 'picking_type_id': recep_type.id,
                    'procure_method': 'make_to_order',
                    'move_orig_ids': [(4, desp_move.id)],
                })
            pickings |= desp_pick
            pickings |= recep_pick

        # 3) confirmar todo (recolección queda Pendiente, lista para "Comenzar recolección")
        pickings.action_confirm()

        # 4) marcar pedidos procesados y vincularlos a la recolección
        pedidos.write({'state': 'procesado', 'picking_recoleccion_id': reco_pick.id})

        # abrir la recolección consolidada
        return {
            'type': 'ir.actions.act_window',
            'name': _('Recolección consolidada'),
            'res_model': 'stock.picking',
            'res_id': reco_pick.id,
            'view_mode': 'form',
            'target': 'current',
        }


class ReabastPedidoLine(models.Model):
    _name = 'yaguven.reabast.pedido.line'
    _description = 'Línea de pedido de reabastecimiento'
    _order = 'pedido_id, sequence, id'

    sequence = fields.Integer(string='Orden', default=10)
    categ_id = fields.Many2one(
        'product.category', string='Rubro', related='product_id.categ_id', readonly=True)

    pedido_id = fields.Many2one(
        'yaguven.reabast.pedido', string='Pedido', required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one(
        'product.product', string='Producto', required=True,
        domain="[('is_storable', '=', True)]")
    product_uom_qty = fields.Float(string='Cantidad', default=1.0, required=True)
    product_uom_id = fields.Many2one(
        'uom.uom', string='UdM', related='product_id.uom_id', readonly=True)

    # --- líneas de mín/máx: la foto del momento en que se armó el pedido (para Central) y el
    #     conteo real de la sucursal. En un pedido manual quedan vacíos.
    orderpoint_id = fields.Many2one(
        'stock.warehouse.orderpoint', string='Regla mín/máx', readonly=True, ondelete='set null')
    conteo_sucursal = fields.Float(string='¿Cuántos hay en el local?')
    contado = fields.Boolean(string='Contado', readonly=True, copy=False,
        help='La sucursal ya anotó cuántos hay (un 0 escrito también cuenta).')
    estado_conteo = fields.Char(string='Estado', compute='_compute_estado_conteo')
    stock_sistema = fields.Float(string='Decía el sistema', readonly=True)
    diferencia = fields.Float(string='Diferencia', compute='_compute_diferencia',
        help='Contó la sucursal menos lo que decía el sistema.')
    en_camino = fields.Float(string='En camino', readonly=True,
        help='Ya pedido o despachado y todavía no recibido en la sucursal.')
    min_qty = fields.Float(string='Mín', readonly=True)
    max_qty = fields.Float(string='Máx', readonly=True)
    stock_central = fields.Float(string='Hay en Central', compute='_compute_stock_central',
        help='Stock hoy en Existencias de Central (y sus sububicaciones).')

    aviso_central = fields.Char(string='Central', compute='_compute_stock_central',
        help='Aviso cuando Central no tiene el producto: no se puede mandar, hay que comprarlo.')

    def _compute_stock_central(self):
        # mismo criterio que el conteo de Central: child_of del origen de la recolección y con
        # todas las empresas activas para no perder valores company_dependent (C.1)
        tipo = self.env['yaguven.reabast.pedido']._tipo_recoleccion()
        loc = tipo.default_location_src_id
        comps = self.env['res.company'].search([]).ids
        grupos = self.env['stock.quant'].with_context(allowed_company_ids=comps)._read_group(
            [('product_id', 'in', self.product_id.ids), ('location_id', 'child_of', loc.id)],
            ['product_id'], ['quantity:sum'])
        por_prod = {prod.id: qty for prod, qty in grupos}
        for ln in self:
            ln.stock_central = por_prod.get(ln.product_id.id, 0.0)
            ln.aviso_central = _('▲ No hay en Central: se compra') \
                if ln.stock_central <= 0 else False

    @api.depends('contado', 'conteo_sucursal', 'stock_sistema')
    def _compute_diferencia(self):
        for ln in self:
            ln.diferencia = ln.conteo_sucursal - ln.stock_sistema if ln.contado else 0.0

    @api.depends('contado')
    def _compute_estado_conteo(self):
        # texto con forma distinta en cada estado: se lee sin depender del color
        for ln in self:
            ln.estado_conteo = _('✔ Listo') if ln.contado else _('● Falta contar')

    def _cantidad_por_conteo(self, conteo):
        """Se pide lo que falta para el máximo, sobre lo que HAY de verdad y lo que ya viene."""
        self.ensure_one()
        return max(self.max_qty - conteo - self.en_camino, 0.0)

    def write(self, vals):
        if 'conteo_sucursal' not in vals:
            return super().write(vals)
        vals = dict(vals, contado=True)
        for ln in self:
            v = dict(vals)
            # Central puede corregir «Se pide» a mano: si lo manda junto, gana lo que escribió
            if ln.orderpoint_id and 'product_uom_qty' not in vals:
                v['product_uom_qty'] = ln._cantidad_por_conteo(vals['conteo_sucursal'])
            super(ReabastPedidoLine, ln).write(v)
        return True
