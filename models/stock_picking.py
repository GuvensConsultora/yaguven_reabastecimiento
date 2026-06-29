from odoo import models, _
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

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
