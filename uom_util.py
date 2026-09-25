def _uom_rounding(env):
    """Paso de redondeo de cantidades (reemplaza a ``uom.uom.rounding``, que no existe en 20).

    En Odoo 20 todas las UdM redondean con la precisión decimal «Product Unit»; la traducimos
    al paso equivalente (2 dígitos -> 0,01), que es el que tenían todas las UdM en lupa3.
    """
    return 10 ** -env['decimal.precision'].precision_get('Product Unit')
