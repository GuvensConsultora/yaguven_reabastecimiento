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
    note = fields.Text(string='Observaciones')
    # vínculo a la recolección consolidada que procesó el pedido (campo en NUESTRO modelo, no en
    # el nativo stock.picking — C.2). Lo setea action_armar_recoleccion (lo invoca el wizard 3b).
    picking_recoleccion_id = fields.Many2one(
        'stock.picking', string='Recolección', readonly=True, copy=False, tracking=True,
        help='Recolección consolidada en la que se procesó este pedido.')
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True,
        default=lambda self: self.env.company)

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
            for ln in ped.line_ids:
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

    pedido_id = fields.Many2one(
        'yaguven.reabast.pedido', string='Pedido', required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one(
        'product.product', string='Producto', required=True,
        domain="[('is_storable', '=', True)]")
    product_uom_qty = fields.Float(string='Cantidad', default=1.0, required=True)
    product_uom_id = fields.Many2one(
        'uom.uom', string='UdM', related='product_id.uom_id', readonly=True)
