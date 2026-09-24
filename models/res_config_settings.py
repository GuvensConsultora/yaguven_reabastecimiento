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

    # ── mín/máx sugeridos por ventas (reabast_minmax.py). Valores de entrada: data/reabast_config.xml
    yaguven_minmax_dias_ventas = fields.Integer(
        string='Días de ventas para calcular',
        config_parameter='yaguven_reabastecimiento.minmax_dias_ventas',
        help='Ventana de ventas que usa el cálculo semanal de mínimos y máximos sugeridos.')
    yaguven_minmax_umbral_mes = fields.Float(
        string='Se mueve desde (unidades por mes)',
        config_parameter='yaguven_reabastecimiento.minmax_umbral_mes',
        help='Debajo de este promedio mensual el producto es de poca venta y lleva el mínimo y '
             'máximo fijos de poca venta.')
    yaguven_minmax_margen_dias = fields.Float(
        string='Días de margen',
        config_parameter='yaguven_reabastecimiento.minmax_margen_dias',
        help='Se suman a los días entre llegadas para calcular el mínimo.')
    yaguven_minmax_factor_max = fields.Float(
        string='Máximo = mínimo por',
        config_parameter='yaguven_reabastecimiento.minmax_factor_max')
    yaguven_minmax_poca_min = fields.Float(
        string='Poca venta: mínimo',
        config_parameter='yaguven_reabastecimiento.minmax_poca_min')
    yaguven_minmax_poca_max = fields.Float(
        string='Poca venta: máximo',
        config_parameter='yaguven_reabastecimiento.minmax_poca_max')
