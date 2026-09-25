import math
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

# Parámetros de la regla (Inventario › Ajustes › Reabastecimiento de sucursales). Los valores de
# entrada los siembra data/reabast_config.xml; acá sólo se leen (B.23: nada del cliente fijo en código).
PARAM = 'yaguven_reabastecimiento.%s'


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    # cada cuántos días le llega mercadería de Central. Editable: lupa3 no tiene historia de
    # remitos para medirlo, y cuando la tenga lo sigue decidiendo Central.
    yaguven_dias_entre_llegadas = fields.Float(
        string='Días entre llegadas de Central',
        help='Cada cuántos días recibe mercadería esta sucursal. El mínimo sugerido cubre la venta '
             'de estos días más el margen. En 0, la sucursal no recibe sugeridos.')


class StockWarehouseOrderpoint(models.Model):
    _inherit = 'stock.warehouse.orderpoint'

    # Sugeridos: el cálculo semanal NO pisa el mínimo y el máximo. Los deja acá, y Central los
    # aplica con «Aplicar sugerido» (todos o algunos). Bajan el stock de las sucursales respecto de
    # lo cargado hoy (medido 24/09), así que el cambio lo decide una persona.
    yaguven_venta_ventana = fields.Float(
        string='Vendido en la ventana', readonly=True,
        help='Unidades vendidas en los días de la ventana de cálculo, neto de devoluciones.')
    yaguven_min_sugerido = fields.Float(string='Mín sugerido', readonly=True)
    yaguven_max_sugerido = fields.Float(string='Máx sugerido', readonly=True)
    yaguven_sugerido_fecha = fields.Date(string='Sugerido el', readonly=True)
    yaguven_sugerido_fuente = fields.Char(
        string='Calculado con', readonly=True,
        help='De dónde salieron las ventas: los movimientos de este sistema, o una carga inicial.')

    # ------------------------------------------------------------------
    @api.model
    def _yaguven_param(self, clave, defecto):
        # Odoo 20: get_param no existe; get_float devuelve el default si falta o no es número.
        return self.env['ir.config_parameter'].sudo().get_float(PARAM % clave, defecto)

    @api.model
    def _yaguven_regla(self, vendido, dias_ventana, dias_llegada):
        """La regla, en un solo lugar (la usan el proceso semanal y la carga inicial).
        Se mueve (umbral o más por mes) → mín = venta diaria × (días entre llegadas + margen),
        máx = factor × mín. Poca venta → mín y máx fijos de «poca venta»."""
        umbral = self._yaguven_param('minmax_umbral_mes', 3)
        margen = self._yaguven_param('minmax_margen_dias', 1)
        factor = self._yaguven_param('minmax_factor_max', 2)
        por_mes = vendido / dias_ventana * 30
        if por_mes < umbral:
            return (self._yaguven_param('minmax_poca_min', 1),
                    self._yaguven_param('minmax_poca_max', 2))
        minimo = math.ceil(vendido / dias_ventana * (dias_llegada + margen))
        return minimo, math.ceil(minimo * factor)

    @api.model
    def _yaguven_sugerir(self, ventas, dias_ventana, fuente):
        """Escribe los sugeridos. `ventas` = {(almacén_id, producto_id): unidades vendidas}.
        Sólo toca reglas que ya existen y se surten desde Central; los productos sin ventas en la
        ventana quedan como están (a definir por el negocio). Devuelve un resumen por sucursal."""
        recoleccion = self.env['stock.picking.type'].search(
            [('yaguven_reabast_paso', '=', 'recoleccion')], limit=1)
        if not recoleccion:
            raise UserError(_("No está configurado el tipo de operación 'Recolección' de reabastecimiento."))
        loc_central = recoleccion.default_location_src_id
        sucursales = self.env['stock.picking.type'].search(
            [('yaguven_reabast_paso', '=', 'recepcion')]).warehouse_id
        hoy = fields.Date.context_today(self)
        resumen = {}
        for suc in sucursales:
            r = resumen[suc.display_name] = defaultdict(int)
            if not suc.yaguven_dias_entre_llegadas:
                r['sin días entre llegadas: no se calculó'] = 1
                continue
            reglas = self.search([('warehouse_id', '=', suc.id),
                                  ('route_id.rule_ids.location_src_id', '=', loc_central.id)])
            por_producto = {op.product_id.id: op for op in reglas}
            for (wid, pid), vendido in ventas.items():
                if wid != suc.id or vendido <= 0:
                    continue
                op = por_producto.get(pid)
                if not op:
                    r['vendido sin regla'] += 1
                    continue
                minimo, maximo = self._yaguven_regla(vendido, dias_ventana, suc.yaguven_dias_entre_llegadas)
                op.write({'yaguven_venta_ventana': vendido, 'yaguven_min_sugerido': minimo,
                          'yaguven_max_sugerido': maximo, 'yaguven_sugerido_fecha': hoy,
                          'yaguven_sugerido_fuente': fuente})
                r['con sugerido'] += 1
        return {k: dict(v) for k, v in resumen.items()}

    @api.model
    def yaguven_sugerir_desde_ventas(self, ventas_lista, dias_ventana, fuente):
        """Carga inicial desde afuera (ventas de O17, que este sistema no tiene).
        `ventas_lista` = [[almacén_id, producto_id, unidades], ...]. Sólo el Supervisor."""
        if not self.env.user.has_group('yaguven_reabastecimiento.group_reabast_supervisor'):
            raise UserError(_("Sólo el Supervisor de reabastecimiento puede cargar sugeridos."))
        ventas = {(w, p): q for w, p, q in ventas_lista}
        return self._yaguven_sugerir(ventas, dias_ventana, fuente)

    @api.model
    def _cron_recalcular_minmax(self):
        """Proceso semanal: ventas de la ventana tomadas de los movimientos de este sistema
        (salidas a clientes menos devoluciones, por sucursal y producto)."""
        dias = int(self._yaguven_param('minmax_dias_ventas', 90))
        desde = fields.Datetime.now() - timedelta(days=dias)
        Move = self.env['stock.move']
        ventas = defaultdict(float)
        for loc, prod, qty in Move._read_group(
                [('state', '=', 'done'), ('date', '>=', desde),
                 ('location_id.usage', '=', 'internal'), ('location_dest_id.usage', '=', 'customer')],
                ['location_id', 'product_id'], ['product_qty:sum']):
            if loc.warehouse_id:
                ventas[(loc.warehouse_id.id, prod.id)] += qty
        for loc, prod, qty in Move._read_group(
                [('state', '=', 'done'), ('date', '>=', desde),
                 ('location_id.usage', '=', 'customer'), ('location_dest_id.usage', '=', 'internal')],
                ['location_dest_id', 'product_id'], ['product_qty:sum']):
            if loc.warehouse_id:
                ventas[(loc.warehouse_id.id, prod.id)] -= qty
        return self._yaguven_sugerir(ventas, dias, _('movimientos de este sistema'))

    def action_yaguven_aplicar_sugerido(self):
        """Pasa el sugerido al mínimo y máximo reales, en las reglas seleccionadas que lo tengan."""
        con = self.filtered(lambda op: op.yaguven_sugerido_fecha)
        for op in con:
            op.write({'product_min_qty': op.yaguven_min_sugerido,
                      'product_max_qty': op.yaguven_max_sugerido})
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'info', 'sticky': False, 'title': _('Sugerido aplicado'),
                       'message': _('%(a)s reglas actualizadas; %(s)s sin sugerido quedaron igual.',
                                    a=len(con), s=len(self) - len(con))},
        }
