from collections import defaultdict

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare
from odoo.tools.misc import html_escape


class ReabastConteoWizard(models.TransientModel):
    """Conteo físico en Central al armar las entregas (sub-ladrillo: diferencias de stock en Central).

    Antes de repartir, el recolector carga lo que HAY DE VERDAD en Central por producto. Si el físico
    difiere del sistema, el wizard genera el AJUSTE DE INVENTARIO nativo (para que sistema = físico) y,
    si tras el ajuste no alcanza para cubrir todos los pedidos, encadena automáticamente el wizard de
    reparto (que ya trabaja sobre el on-hand corregido). El asiento contable del ajuste lo produce la
    valuación real-time de la categoría (RT 59 / C.5): no inventamos asiento a mano.
    """
    _name = 'yaguven.reabast.conteo.wizard'
    _description = 'Conteo físico en Central antes de repartir'

    picking_id = fields.Many2one('stock.picking', string='Recolección', required=True, readonly=True)
    line_ids = fields.One2many('yaguven.reabast.conteo.wizard.line', 'wizard_id', string='Conteo')

    # ------------------------------------------------------------------
    @api.model
    def _onhand_central(self, picking, producto):
        """On-hand físico (sistema) del producto en el origen de la recolección (Central/Existencias).
        Mismo criterio que el wizard de reparto (child_of del origen) y con todas las empresas activas
        para no perder valores company_dependent (C.1)."""
        loc = picking.picking_type_id.default_location_src_id
        comps = self.env['res.company'].search([]).ids
        quants = self.env['stock.quant'].with_context(allowed_company_ids=comps).search([
            ('product_id', '=', producto.id),
            ('location_id', 'child_of', loc.id),
        ])
        return sum(quants.mapped('quantity'))

    @api.model
    def _pedido_por_producto(self, picking):
        """Demanda consolidada por producto (= total pedido) desde los moves de la recolección."""
        pedido = defaultdict(float)
        for mv in picking.move_ids.filtered(lambda m: m.state != 'cancel'):
            pedido[mv.product_id] += mv.product_uom_qty
        return pedido

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        picking_id = self.env.context.get('active_id') or res.get('picking_id')
        if not picking_id:
            return res
        picking = self.env['stock.picking'].browse(picking_id)
        if picking.picking_type_id.yaguven_reabast_paso != 'recoleccion':
            raise UserError(_("El conteo se hace sobre una recolección de reabastecimiento."))
        res['picking_id'] = picking.id
        lines = []
        for prod, ped in self._pedido_por_producto(picking).items():
            sistema = self._onhand_central(picking, prod)
            lines.append((0, 0, {
                'producto_id': prod.id,
                'pedido_qty': ped,
                'sistema_qty': sistema,
                'contado_qty': sistema,   # default: lo que dice el sistema (el operador corrige)
            }))
        res['line_ids'] = lines
        return res

    # ------------------------------------------------------------------
    def action_aplicar(self):
        self.ensure_one()
        picking = self.picking_id
        if picking.picking_type_id.yaguven_reabast_paso != 'recoleccion':
            raise UserError(_("El conteo se hace sobre una recolección de reabastecimiento."))
        company = picking.picking_type_id.company_id
        loc = picking.picking_type_id.default_location_src_id
        Quant = self.env['stock.quant'].with_company(company)

        ajustes = []   # (producto, sistema, contado, diferencia) para la traza
        for ln in self.line_ids:
            prod = ln.producto_id
            rounding = prod.uom_id.rounding or 0.01
            # relee el on-hand VIVO: idempotencia (si ya coincide con lo contado, no re-ajusta -> no
            # duplica el asiento de diferencia de inventario) (B.7 / B.14)
            sistema_vivo = self._onhand_central(picking, prod)
            if float_compare(ln.contado_qty, sistema_vivo, precision_rounding=rounding) == 0:
                continue
            # ajuste de inventario NATIVO (conteo físico): setear la cantidad contada y aplicar
            quant = Quant.with_context(inventory_mode=True).search([
                ('product_id', '=', prod.id), ('location_id', '=', loc.id),
            ], limit=1)
            if quant:
                quant.inventory_quantity = ln.contado_qty
            else:
                quant = Quant.with_context(inventory_mode=True).create({
                    'product_id': prod.id, 'location_id': loc.id,
                    'inventory_quantity': ln.contado_qty,
                })
            quant.action_apply_inventory()
            ajustes.append((prod, sistema_vivo, ln.contado_qty, ln.contado_qty - sistema_vivo))

        if ajustes:
            filas = ''.join(
                "<tr><td>%s</td><td style='text-align:right'>%s</td>"
                "<td style='text-align:right'>%s</td><td style='text-align:right'>%s</td></tr>" % (
                    html_escape(p.display_name), html_escape('%g' % sis),
                    html_escape('%g' % con), html_escape('%+g' % dif))
                for (p, sis, con, dif) in ajustes)
            body = (
                "<p><strong>Conteo físico en Central — ajuste de inventario</strong></p>"
                "<table border='1' cellpadding='4' style='border-collapse:collapse'>"
                "<tr><th>Producto</th><th>Sistema</th><th>Contado</th><th>Diferencia</th></tr>"
                "%s</table>"
                "<p>Se ajustó el inventario de Central a lo contado; el stock del sistema ahora "
                "refleja el físico.</p>") % filas
            picking.message_post(
                body=Markup(body), subject=_("Conteo físico en Central"),
                message_type="comment", subtype_xmlid="mail.mt_note")

        # ¿queda faltante tras el ajuste? -> abrir el reparto automáticamente (reparto sobre lo real)
        hay_faltante = False
        for prod, ped in self._pedido_por_producto(picking).items():
            disp = self._onhand_central(picking, prod)
            rounding = prod.uom_id.rounding or 0.01
            if float_compare(disp, ped, precision_rounding=rounding) < 0:
                hay_faltante = True
                break

        if hay_faltante:
            action = self.env['ir.actions.actions']._for_xml_id(
                'yaguven_reabastecimiento.action_reabast_faltante')
            action['context'] = {'active_id': picking.id, 'active_model': 'stock.picking'}
            return action
        return {'type': 'ir.actions.act_window_close'}


class ReabastConteoWizardLine(models.TransientModel):
    _name = 'yaguven.reabast.conteo.wizard.line'
    _description = 'Línea de conteo físico en Central'

    wizard_id = fields.Many2one(
        'yaguven.reabast.conteo.wizard', string='Wizard', required=True, ondelete='cascade')
    producto_id = fields.Many2one('product.product', string='Producto', readonly=True)
    pedido_qty = fields.Float(string='Pedido (total)', readonly=True)
    sistema_qty = fields.Float(string='Sistema', readonly=True)
    contado_qty = fields.Float(string='Contado')
    diferencia = fields.Float(string='Diferencia', compute='_compute_diferencia')
    uom_name = fields.Char(related='producto_id.uom_id.name', string='UdM', readonly=True)

    @api.depends('contado_qty', 'sistema_qty')
    def _compute_diferencia(self):
        for ln in self:
            ln.diferencia = ln.contado_qty - ln.sistema_qty
