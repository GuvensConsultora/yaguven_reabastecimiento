from odoo import models


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _search_picking_for_assignation_domain(self):
        """Cutoff de concurrencia: una recolección 'En recolección' (congelada) deja de ser
        candidata para fusionar movimientos entrantes. Así, un pedido nuevo que llega mientras
        Central arma una recolección no se suma a ese documento, sino que arma uno nuevo.

        Hook mínimo (C.2): solo se EXTIENDE el dominio nativo que stock.move usa para buscar un
        picking donde consolidar (confirmado en el source de Odoo 19); no se reescribe la lógica
        de consolidación. Afecta únicamente a los pickings con el flag puesto, que solo se setea
        en recolecciones del reabastecimiento.
        """
        domain = super()._search_picking_for_assignation_domain()
        return domain + [('yaguven_en_recoleccion', '=', False)]
