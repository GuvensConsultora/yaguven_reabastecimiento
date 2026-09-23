from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ReabastMinmaxWizard(models.TransientModel):
    """«Pedir mercadería» cuando el usuario maneja varias sucursales (Central): elige para cuáles
    se arma el pedido de mín/máx. Cada sucursal recibe SU pedido en borrador y lo cuenta ella."""
    _name = 'yaguven.reabast.minmax.wizard'
    _description = 'Pedir mercadería por mín/máx para varias sucursales'

    permitidas_ids = fields.Many2many(
        'stock.warehouse', 'reabast_minmax_wizard_permitidas_rel', string='Sucursales posibles',
        readonly=True)
    sucursal_ids = fields.Many2many(
        'stock.warehouse', 'reabast_minmax_wizard_sucursal_rel', string='Sucursales',
        domain="[('id', 'in', permitidas_ids)]")

    def action_confirmar(self):
        self.ensure_one()
        if not self.sucursal_ids:
            raise UserError(_("Elegí al menos una sucursal."))
        Pedido = self.env['yaguven.reabast.pedido']
        nuevos, existentes = Pedido._traer_minmax(self.sucursal_ids)
        pedidos = nuevos | existentes
        if not pedidos:
            raise UserError(_("Según el sistema, a ninguna de esas sucursales le falta nada hoy."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pedidos de mín/máx: %(n)s nuevos, %(e)s ya estaban empezados',
                      n=len(nuevos), e=len(existentes)),
            'res_model': 'yaguven.reabast.pedido',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pedidos.ids)],
            'target': 'current',
        }


class ReabastPedidoMinmax(models.Model):
    _inherit = 'yaguven.reabast.pedido'

    @api.model
    def _accion_elegir_sucursales(self, sucursales):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pedir mercadería'),
            'res_model': 'yaguven.reabast.minmax.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_permitidas_ids': [(6, 0, sucursales.ids)],
                        'default_sucursal_ids': [(6, 0, sucursales.ids)]},
        }
