from odoo import fields, models


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    yaguven_reabast_paso = fields.Selection(
        [('recoleccion', 'Recolección'),
         ('despacho', 'Despacho'),
         ('recepcion', 'Recepción')],
        string='Paso de reabastecimiento',
        help='Marca este tipo de operación como un paso del circuito de reabastecimiento. '
             'Un paso solo puede validarse cuando el paso anterior de la cadena está hecho '
             '(recolección → despacho → recepción).')
