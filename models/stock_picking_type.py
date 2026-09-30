import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    yaguven_reabast_paso = fields.Selection(
        [('recoleccion', 'Recolección'),
         ('despacho', 'Despacho'),
         ('recepcion', 'Recepción'),
         ('devolucion', 'Devolución'),
         ('envio', 'Envío entre sucursales')],
        string='Paso de reabastecimiento',
        help='Marca este tipo de operación como un paso del circuito de reabastecimiento. '
             'Un paso solo puede validarse cuando el paso anterior de la cadena está hecho '
             '(recolección → despacho → recepción). "Devolución" es el traslado de retorno '
             'sucursal → central que genera la resolución de una diferencia de recepción (5b). '
             '"Envío entre sucursales" es la salida de una sucursal hacia el tránsito de otra: '
             'la sucursal que recibe lo recepciona con su tipo de Recepción de siempre.')

    # --- Envío entre sucursales -------------------------------------------------------------
    # Las sucursales habilitadas se derivan de la topología, no de ids fijos (B.23): son los
    # almacenes que tienen tipo de Recepción de reabastecimiento, menos Central (el almacén de la
    # Recolección), que sigue siempre por el circuito completo.

    # Los almacenes tienen regla por Unidad Operativa (una sucursal sólo lee el suyo), así que la
    # topología se lee con sudo: devuelve almacenes en sudo, para usar sus ids y no para mostrarlos.

    @api.model
    def _yg_sucursales_envio(self):
        tipos = self.sudo()
        recep = tipos.search([('yaguven_reabast_paso', '=', 'recepcion')])
        central = tipos.search([('yaguven_reabast_paso', '=', 'recoleccion')]).warehouse_id
        return (recep.warehouse_id - central).sorted('name')

    @api.model
    def _yg_tipo_envio(self, sucursal):
        """Tipo «REAB Envío desde <sucursal>»: lo busca y, si no existe, lo crea. Sale de las
        existencias de la sucursal; el destino (el tránsito de la que recibe) lo fija cada envío."""
        sucursal = sucursal.sudo()
        tipo = self.search([('yaguven_reabast_paso', '=', 'envio'),
                            ('warehouse_id', '=', sucursal.id)], limit=1)
        if tipo:
            return tipo
        if sucursal.id not in self._yg_sucursales_envio().ids:
            raise UserError(_("«%s» no está habilitada para envíos entre sucursales.")
                            % sucursal.display_name)
        code = re.sub(r'[^A-Z0-9]', '', (sucursal.code or '').upper())[:5]
        return self.sudo().create({
            'name': _('REAB Envío desde %s') % sucursal.name.split('- ')[-1],
            'code': 'internal',
            'warehouse_id': sucursal.id,
            'company_id': sucursal.company_id.id,
            'default_location_src_id': sucursal.lot_stock_id.id,
            'default_location_dest_id': sucursal.lot_stock_id.id,
            'sequence_code': 'ENV' + code,
            'yaguven_reabast_paso': 'envio',
        })
