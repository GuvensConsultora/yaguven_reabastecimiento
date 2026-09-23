from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ReabastArmarWizard(models.TransientModel):
    _name = 'yaguven.reabast.armar.wizard'
    _description = 'Armar recolección de reabastecimiento'

    # Pantalla de confirmación: lista los pedidos enviados que se van a consolidar. El scoping por
    # Unidad Operativa lo aplica la regla de registro del pedido (el usuario ve solo lo suyo).
    pedido_ids = fields.Many2many(
        'yaguven.reabast.pedido', string='Pedidos a consolidar',
        domain="[('state', '=', 'enviado')]",
        default=lambda self: self._default_pedidos())
    cantidad = fields.Integer(string='Cantidad de pedidos', compute='_compute_resumen')
    sucursales = fields.Integer(string='Sucursales', compute='_compute_resumen')
    # Los pendientes de recolecciones anteriores (backorder del reparto, o el faltante que Central
    # autorizó) nacen en BORRADOR a propósito: la sucursal decide de nuevo si todavía lo necesita.
    # El riesgo es que queden dormidos, porque «Armar recolección» solo toma los «Enviado» y nada
    # los recuerda. Se listan acá, que es el momento en que la decisión importa. No se envían
    # solos: despachar algo que ya no hace falta es peor que no despacharlo.
    borrador_ids = fields.Many2many(
        'yaguven.reabast.pedido', 'reabast_armar_wizard_borrador_rel',
        string='Pendientes en borrador (NO se incluyen)', readonly=True,
        default=lambda self: self._default_borradores())
    borradores = fields.Integer(string='Pendientes', compute='_compute_resumen')

    @api.model
    def _default_pedidos(self):
        return self.env['yaguven.reabast.pedido'].search([('state', '=', 'enviado')])

    @api.model
    def _default_borradores(self):
        return self.env['yaguven.reabast.pedido'].search([('state', '=', 'borrador')])

    @api.depends('pedido_ids', 'borrador_ids')
    def _compute_resumen(self):
        for wiz in self:
            wiz.cantidad = len(wiz.pedido_ids)
            wiz.sucursales = len(wiz.pedido_ids.mapped('sucursal_id'))
            wiz.borradores = len(wiz.borrador_ids)

    def action_armar(self):
        self.ensure_one()
        if not self.pedido_ids:
            raise UserError(_("No hay pedidos seleccionados para armar la recolección."))
        # delega en el motor del modelo Pedido (filtra 'enviado' y corta si no hay ninguno)
        return self.pedido_ids.action_armar_recoleccion()
