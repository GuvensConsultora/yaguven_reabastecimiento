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
