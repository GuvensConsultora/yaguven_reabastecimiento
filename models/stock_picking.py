from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    # --- Estado intermedio "En recolección" + cutoff (sub-ladrillo 2d) ---
    # El flag vive en el picking a propósito (C.2): el cutoff se aplica en el dominio que
    # stock.move._search_picking_for_assignation_domain arma SOBRE stock.picking; un m2o externo
    # obligaría un join en código nativo. Solo tiene sentido en pickings de paso 'recoleccion'.
    yaguven_en_recoleccion = fields.Boolean(
        string='En recolección (congelada)',
        default=False, copy=False,
        help='Cuando está activo, esta recolección quedó congelada: no absorbe pedidos nuevos '
             '(los pedidos que llegan después arman una recolección nueva).')

    yaguven_recoleccion_estado = fields.Selection(
        [('pendiente', 'Pendiente'),
         ('en_recoleccion', 'En recolección'),
         ('hecha', 'Hecha')],
        string='Estado de recolección',
        compute='_compute_yaguven_recoleccion_estado',
        help='Ciclo de la recolección: Pendiente (abierta a nuevos pedidos) → En recolección '
             '(congelada) → Hecha (validada; habilita el despacho).')

    @api.depends('state', 'yaguven_en_recoleccion', 'picking_type_id.yaguven_reabast_paso')
    def _compute_yaguven_recoleccion_estado(self):
        for picking in self:
            if picking.picking_type_id.yaguven_reabast_paso != 'recoleccion':
                picking.yaguven_recoleccion_estado = False
            elif picking.state == 'done':
                picking.yaguven_recoleccion_estado = 'hecha'
            elif picking.yaguven_en_recoleccion:
                picking.yaguven_recoleccion_estado = 'en_recoleccion'
            else:
                picking.yaguven_recoleccion_estado = 'pendiente'

    def action_yaguven_comenzar_recoleccion(self):
        """Congela la recolección: a partir de acá los pedidos nuevos no se fusionan a este
        documento (cutoff aplicado en stock.move._search_picking_for_assignation_domain)."""
        for picking in self:
            if picking.picking_type_id.yaguven_reabast_paso != 'recoleccion':
                raise UserError(_("Solo se puede comenzar una recolección del reabastecimiento."))
            if picking.state in ('done', 'cancel'):
                raise UserError(_("Esta recolección ya no se puede comenzar (está %s).") % picking.state)
            if picking.yaguven_en_recoleccion:
                continue
            picking.yaguven_en_recoleccion = True
            body = ("<p><strong>Recolección comenzada.</strong></p>"
                    "<p>Este documento queda congelado: los pedidos nuevos arman una recolección "
                    "nueva, no se suman a éste.</p>")
            picking.message_post(
                body=Markup(body),
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
        return True

    def button_validate(self):
        """Gateo del circuito de reabastecimiento: un paso no se puede validar si el paso
        anterior de la cadena (sus movimientos de origen) no está hecho. Reemplaza el error
        nativo críptico ('no se puede validar sin reservas') por un mensaje claro.

        Solo actúa sobre pickings cuyo tipo está marcado como paso (yaguven_reabast_paso);
        el resto valida igual que siempre (no invasivo, C.2).
        """
        for picking in self:
            paso = picking.picking_type_id.yaguven_reabast_paso
            if not paso:
                continue
            # pickings de origen = el/los paso(s) anterior(es) en la cadena make_to_order
            origen = picking.move_ids.move_orig_ids.picking_id.filtered(lambda p: p.id != picking.id)
            pendientes = origen.filtered(lambda p: p.state not in ('done', 'cancel'))
            if pendientes:
                anterior = {'despacho': 'la recolección',
                            'recepcion': 'el despacho'}.get(paso, 'el paso anterior')
                etiqueta = {'recoleccion': 'la recolección',
                            'despacho': 'el despacho',
                            'recepcion': 'la recepción'}.get(paso, 'este paso')
                raise UserError(_(
                    "No se puede validar %s todavía. Primero confirmá %s.\n\n"
                    "Pendiente: %s"
                ) % (etiqueta, anterior, ', '.join(pendientes.mapped('name'))))
        return super().button_validate()
