from collections import defaultdict

from odoo import fields, models, _
from odoo.exceptions import UserError


class StockWarehouseOrderpoint(models.Model):
    """«Ordenar» manual, como en la 17, pero entrando al circuito de reabastecimiento.

    Pedido de Anael (25/09): la reposición a sucursales es MANUAL —se aprieta «Ordenar» en
    Reabastecimiento, producto por producto— y lo pedido tiene que pasar a «Armar recolección». Sin
    conteo en sucursal y sin nada automático.

    En las reglas que se surten desde Central, «Ordenar» ya no crea remitos: suma la línea al pedido
    abierto de la sucursal (enviado, origen «Desde Reabastecimiento»), y Central lo consolida con
    «Armar recolección». Las demás reglas (compras, otras rutas) siguen con el comportamiento nativo.
    """
    _inherit = 'stock.warehouse.orderpoint'

    def _yaguven_reab(self):
        """Las reglas de self que pertenecen al circuito: sucursal con recepción de reabastecimiento
        y ruta que se surte desde Existencias de Central. Resuelto por configuración, sin ids."""
        Tipo = self.env['stock.picking.type'].sudo()
        reco = Tipo.search([('yaguven_reabast_paso', '=', 'recoleccion')], limit=1)
        if not reco:
            return self.browse()
        loc_central = reco.default_location_src_id
        sucursales = Tipo.search([('yaguven_reabast_paso', '=', 'recepcion')]).warehouse_id
        return self.filtered(lambda op: op.warehouse_id in sucursales
                             and loc_central in op.route_id.rule_ids.location_src_id)

    def _quantity_in_progress(self):
        """Lo que ya está pedido y todavía no se armó cuenta como «en camino»: así el producto deja de
        figurar para ordenar y no se pide dos veces. (Hook nativo: «cantidades que todavía no están
        en el stock previsto pero hay que descontar»). Una vez armada la recolección, los movimientos
        ya los cuenta el stock previsto y el pedido pasa a «procesado», así que no se suma de nuevo."""
        res = super()._quantity_in_progress()
        reab = self._yaguven_reab()
        if not reab:
            return res
        lineas = self.env['yaguven.reabast.pedido.line'].sudo().search([
            ('pedido_id.state', 'in', ('borrador', 'enviado')),
            ('pedido_id.sucursal_id', 'in', reab.warehouse_id.ids),
            ('product_id', 'in', reab.product_id.ids)])
        pedido = defaultdict(float)
        for ln in lineas:
            pedido[(ln.pedido_id.sucursal_id.id, ln.product_id.id)] += ln.product_uom_qty
        for op in reab:
            res[op.id] = res.get(op.id, 0.0) + pedido[(op.warehouse_id.id, op.product_id.id)]
        return res

    def action_replenish(self, force_to_max=False):
        reab = self._yaguven_reab()
        otras = self - reab
        res = super(StockWarehouseOrderpoint, otras).action_replenish(force_to_max=force_to_max) \
            if otras else False
        if not reab:
            return res
        if force_to_max:
            for op in reab:
                op.qty_to_order = op._get_multiple_rounded_qty(op.product_max_qty - op.qty_forecast)
        pedidos = reab._yaguven_agregar_a_pedido()
        reab.action_remove_manual_qty_to_order()
        reab._compute_qty_to_order()
        if not pedidos:
            raise UserError(_("No hay nada para pedir: la cantidad a ordenar es 0."))
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'type': 'info', 'sticky': False, 'title': _('Agregado al pedido'),
                'message': ', '.join(f'{p.name} ({p.sucursal_id.display_name})' for p in pedidos)
                           + _('. Central lo prepara en la próxima «Armar recolección».'),
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},   # refresca la lista
            },
        }

    def action_replenish_auto(self):
        if self._yaguven_reab():
            raise UserError(_(
                "La reposición a las sucursales es manual: usá «Ordenar» y el pedido pasa a "
                "«Armar recolección»."))
        return super().action_replenish_auto()

    def _yaguven_agregar_a_pedido(self):
        """Suma cada regla a UN pedido abierto por sucursal (origen «Desde Reabastecimiento», enviado
        y sin armar). Si el producto ya está en ese pedido, suma la cantidad a la misma línea."""
        Pedido = self.env['yaguven.reabast.pedido']
        pedidos = Pedido.browse()
        for sucursal in self.warehouse_id:
            ops = self.filtered(lambda o: o.warehouse_id == sucursal and o.qty_to_order > 0)
            if not ops:
                continue
            pedido = Pedido.search([('sucursal_id', '=', sucursal.id), ('origen', '=', 'orden'),
                                    ('state', '=', 'enviado')], limit=1)
            if not pedido:
                pedido = Pedido.create({'sucursal_id': sucursal.id, 'origen': 'orden',
                                        'company_id': sucursal.company_id.id, 'state': 'enviado'})
            base = max(pedido.line_ids.mapped('sequence') or [0])
            for i, op in enumerate(ops, 1):
                ln = pedido.line_ids.filtered(lambda l: l.product_id == op.product_id)[:1]
                if ln:
                    ln.product_uom_qty += op.qty_to_order
                    continue
                pedido.line_ids = [(0, 0, {
                    'sequence': base + i, 'product_id': op.product_id.id, 'orderpoint_id': op.id,
                    'product_uom_qty': op.qty_to_order, 'stock_sistema': op.qty_on_hand,
                    'min_qty': op.product_min_qty, 'max_qty': op.product_max_qty,
                })]
            pedido.message_post(body=_("Desde Reabastecimiento (%(u)s): %(n)s productos.",
                                       u=self.env.user.name, n=len(ops)))
            pedidos |= pedido
        return pedidos
