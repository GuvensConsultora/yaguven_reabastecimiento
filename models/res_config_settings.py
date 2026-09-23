from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # tope de productos por pedido de mín/máx: lo que una persona de sucursal cuenta de una vez.
    # Parámetro del sistema (no fijo en código, B.23); el valor inicial lo siembra data/.
    yaguven_reabast_tope_minmax = fields.Integer(
        string='Productos por pedido de mín/máx',
        config_parameter='yaguven_reabastecimiento.tope_minmax',
        help='Cuántos productos le aparecen a la sucursal para contar en cada pedido de '
             'mín/máx. Los que quedan afuera salen en el próximo pedido. 0 = sin tope.')
