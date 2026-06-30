from collections import defaultdict

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round
from odoo.tools.misc import html_escape


class ReabastFaltanteWizard(models.TransientModel):
    _name = 'yaguven.reabast.faltante.wizard'
    _description = 'Resolver reparto de reabastecimiento (faltante)'

    picking_id = fields.Many2one('stock.picking', string='Recolección', required=True, readonly=True)
    estrategia = fields.Selection(
        [('prorrateo', 'Prorrateo proporcional al pedido'),
         ('prioridad', 'Prioridad por orden de sucursal'),
         ('iguales', 'Partes iguales')],
        string='Estrategia sugerida', default='prorrateo', required=True,
        help='Criterio para sugerir el reparto cuando el disponible no alcanza. Podés ajustar a '
             'mano cada cantidad después.')
    line_ids = fields.One2many(
        'yaguven.reabast.faltante.wizard.line', 'wizard_id', string='Reparto')

    # ------------------------------------------------------------------
    # Construcción inicial (default_get) + recálculo por estrategia (onchange)
    # ------------------------------------------------------------------
    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        picking_id = self.env.context.get('active_id') or res.get('picking_id')
        if picking_id:
            picking = self.env['stock.picking'].browse(picking_id)
            if picking.picking_type_id.yaguven_reabast_paso != 'recoleccion':
                raise UserError(_("El reparto se resuelve sobre una recolección de reabastecimiento."))
            res['picking_id'] = picking.id
            res['line_ids'] = self._build_lines(picking, res.get('estrategia') or 'prorrateo')
        return res

    @api.onchange('estrategia')
    def _onchange_estrategia(self):
        por_prod = defaultdict(list)
        for ln in self.line_ids:
            por_prod[ln.producto_id].append(ln)
        for prod, lns in por_prod.items():
            disp = lns[0].disponible
            rounding = prod.uom_id.rounding or 1.0
            items = [{'key': ln, 'pedido': ln.pedido_qty, 'orden': ln.sucursal_id.id or 0} for ln in lns]
            asign = self._calc_reparto(self.estrategia, disp, items, rounding)
            for ln in lns:
                ln.asignado_qty = asign[ln]

    def _disponible(self, picking, producto):
        """On-hand físico del producto en el origen de la recolección (Central/Existencias)."""
        quants = self.env['stock.quant'].with_company(picking.company_id).search([
            ('location_id', '=', picking.location_id.id), ('product_id', '=', producto.id)])
        return sum(quants.mapped('quantity'))

    def _warehouse_de_transito(self, transito):
        """Sucursal (almacén) dueña de una ubicación de tránsito, vía su tipo de recepción."""
        recep = self.env['stock.picking.type'].search([
            ('yaguven_reabast_paso', '=', 'recepcion'),
            ('default_location_src_id', '=', transito.id)], limit=1)
        return recep.warehouse_id

    def _build_lines(self, picking, estrategia):
        por_prod = defaultdict(list)  # producto -> [despacho moves]
        for mv in picking.move_ids.filtered(lambda m: m.state != 'cancel'):
            for dmv in mv.move_dest_ids.filtered(lambda d: d.state != 'cancel'):
                por_prod[mv.product_id].append(dmv)
        vals = []
        for prod, dmoves in por_prod.items():
            disp = self._disponible(picking, prod)
            rounding = prod.uom_id.rounding or 1.0
            items = [{'key': d.id, 'pedido': d.product_uom_qty,
                      'orden': (self._warehouse_de_transito(d.location_dest_id).id or 0)} for d in dmoves]
            asign = self._calc_reparto(estrategia, disp, items, rounding)
            for d in dmoves:
                wh = self._warehouse_de_transito(d.location_dest_id)
                vals.append((0, 0, {
                    'producto_id': prod.id,
                    'sucursal_id': wh.id if wh else False,
                    'despacho_move_id': d.id,
                    'pedido_qty': d.product_uom_qty,
                    'disponible': disp,
                    'asignado_qty': asign[d.id],
                }))
        return vals

    # ------------------------------------------------------------------
    # Estrategias de reparto. items = [{'key','pedido','orden'}]; devuelve {key: cantidad}.
    # Si el disponible alcanza, asigna el pedido completo (sin faltante).
    # ------------------------------------------------------------------
    def _calc_reparto(self, estrategia, disponible, items, rounding):
        total = sum(it['pedido'] for it in items)
        if total <= 0:
            return {it['key']: 0.0 for it in items}
        if float_compare(disponible, total, precision_rounding=rounding) >= 0:
            return {it['key']: it['pedido'] for it in items}

        if estrategia == 'prioridad':
            res, restante = {}, disponible
            for it in sorted(items, key=lambda i: i['orden']):
                take = float_round(min(it['pedido'], max(restante, 0.0)), precision_rounding=rounding)
                res[it['key']] = take
                restante -= take
            return res

        if estrategia == 'iguales':
            res = {it['key']: 0.0 for it in items}
            pend, restante, guard = list(items), disponible, 0
            while float_compare(restante, 0.0, precision_rounding=rounding) > 0 and pend and guard < 1000:
                guard += 1
                cuota = restante / len(pend)
                sigue = []
                for it in pend:
                    falta = it['pedido'] - res[it['key']]
                    dar = float_round(min(cuota, falta), precision_rounding=rounding)
                    res[it['key']] += dar
                    restante -= dar
                    if it['pedido'] - res[it['key']] >= rounding:
                        sigue.append(it)
                if cuota <= 0:
                    break
                pend = sigue
            return res

        # prorrateo (default): proporcional al pedido, piso por UdM + remanente a las de mayor fracción
        raw = {it['key']: disponible * it['pedido'] / total for it in items}
        res = {k: float_round(v, precision_rounding=rounding, rounding_method='DOWN') for k, v in raw.items()}
        rem = float_round(disponible - sum(res.values()), precision_rounding=rounding)
        pedido_de = {it['key']: it['pedido'] for it in items}
        for k in sorted(raw, key=lambda k: raw[k] - res[k], reverse=True):
            if float_compare(rem, 0.0, precision_rounding=rounding) <= 0:
                break
            if pedido_de[k] - res[k] >= rounding:
                res[k] += rounding
                rem -= rounding
        return res

    # ------------------------------------------------------------------
    # Aplicar el reparto: escribe los despachos, recalcula la recolección, regenera backorder
    # ------------------------------------------------------------------
    def action_aplicar(self):
        self.ensure_one()
        picking = self.picking_id
        if picking.state in ('done', 'cancel'):
            raise UserError(_("Esta recolección ya no admite cambios de reparto (está %s).") % picking.state)
        company = picking.company_id

        falt_por_suc = defaultdict(list)   # sucursal -> [(producto, faltante)]
        asign_por_prod = defaultdict(float)
        # El cliente web, al hacer submit de la lista editable del wizard, puede inyectar una línea
        # fantasma sin producto/despacho (producto_id=False, asignado>0) que rompería la constraint
        # de abajo ("asignar más que lo pedido en False para False"). Cada línea real nace de un
        # despacho (despacho_move_id en _build_lines) -> filtramos por eso y descartamos la espuria.
        for ln in self.line_ids.filtered('despacho_move_id'):
            rounding = ln.producto_id.uom_id.rounding or 1.0
            asign = ln.asignado_qty
            if float_compare(asign, 0.0, precision_rounding=rounding) < 0:
                raise UserError(_("La cantidad asignada no puede ser negativa (%s).") % ln.producto_id.display_name)
            if float_compare(asign, ln.pedido_qty, precision_rounding=rounding) > 0:
                raise UserError(_("No podés asignar más que lo pedido en %s para %s.")
                                % (ln.producto_id.display_name, ln.sucursal_id.display_name))
            ln.despacho_move_id.with_company(company).write({'product_uom_qty': asign})
            asign_por_prod[ln.producto_id] += asign
            falt = ln.pedido_qty - asign
            if float_compare(falt, 0.0, precision_rounding=rounding) > 0 and ln.sucursal_id:
                falt_por_suc[ln.sucursal_id].append((ln.producto_id, falt))

        # la recolección mueve lo realmente repartido (un move por producto = suma asignada)
        for mv in picking.move_ids.filtered(lambda m: m.state != 'cancel'):
            if mv.product_id in asign_por_prod:
                mv.with_company(company).write({'product_uom_qty': asign_por_prod[mv.product_id]})

        # backorder: un pedido nuevo (borrador) por sucursal con el faltante no despachado
        Pedido = self.env['yaguven.reabast.pedido']
        nuevos = Pedido
        for suc, faltas in falt_por_suc.items():
            nuevos |= Pedido.create({
                'sucursal_id': suc.id,
                'note': _('Backorder de %s — faltante no despachado') % picking.name,
                'line_ids': [(0, 0, {'product_id': p.id, 'product_uom_qty': q}) for p, q in faltas],
            })

        if nuevos:
            filas = ''.join('<li>%s</li>' % html_escape(p.display_name) for p in nuevos)
            body = (_("<p><strong>Reparto aplicado con faltante.</strong></p>"
                      "<p>Se generaron pedidos de backorder (borrador) por lo no despachado:</p>"
                      "<ul>%s</ul>") % filas)
            picking.message_post(body=Markup(body), message_type="comment",
                                 subtype_xmlid="mail.mt_note")

        return {'type': 'ir.actions.act_window_close'}


class ReabastFaltanteWizardLine(models.TransientModel):
    _name = 'yaguven.reabast.faltante.wizard.line'
    _description = 'Línea de reparto de reabastecimiento'
    _order = 'producto_id, sucursal_id'

    wizard_id = fields.Many2one('yaguven.reabast.faltante.wizard', required=True, ondelete='cascade')
    producto_id = fields.Many2one('product.product', string='Producto', readonly=True)
    sucursal_id = fields.Many2one('stock.warehouse', string='Sucursal', readonly=True)
    despacho_move_id = fields.Many2one('stock.move', string='Despacho', readonly=True)
    pedido_qty = fields.Float(string='Pedido', readonly=True)
    disponible = fields.Float(string='Disponible en Central', readonly=True,
                              help='On-hand del producto en Central/Existencias (compartido por producto).')
    asignado_qty = fields.Float(string='Asignado')
    faltante_qty = fields.Float(string='Faltante', compute='_compute_faltante')

    @api.depends('pedido_qty', 'asignado_qty')
    def _compute_faltante(self):
        for ln in self:
            ln.faltante_qty = max(ln.pedido_qty - ln.asignado_qty, 0.0)
