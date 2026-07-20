from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.misc import html_escape


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    # --- Paso del circuito, expuesto para las vistas (sub-ladrillo 5) ---
    # Related de solo lectura al tipo de operación: las vistas no pueden evaluar un dotted-path
    # (picking_type_id.yaguven_reabast_paso) en un `invisible`, así que se expone acá para gobernar
    # la visibilidad del botón "Informar diferencias" (y futuros por paso). No es dato nuevo (C.2).
    yaguven_reabast_paso = fields.Selection(
        related='picking_type_id.yaguven_reabast_paso', string='Paso de reabastecimiento',
        readonly=True)

    # --- Reporte de diferencias de recepción (sub-ladrillo 5) ---
    yaguven_diferencia_id = fields.Many2one(
        'yaguven.reabast.diferencia', string='Diferencias informadas', copy=False, readonly=True,
        help='Reporte de diferencias de esta recepción (si la sucursal informó alguna).')

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

    # --- Hoja consolidada legible (sub-ladrillo 3c) ---
    # Matriz producto × sucursal computada de la cadena (recolección → despachos → tránsito de cada
    # sucursal). Solo en pickings de paso 'recoleccion'. Read-only: es vista, no dato editable.
    yaguven_hoja_consolidada = fields.Html(
        string='Hoja consolidada', compute='_compute_yaguven_hoja_consolidada',
        sanitize=False, readonly=True,
        help='Qué preparar (total por producto) y adónde va (cantidad por sucursal).')

    @api.depends('move_ids', 'move_ids.product_id', 'move_ids.product_uom_qty',
                 'move_ids.move_dest_ids', 'move_ids.move_dest_ids.product_uom_qty',
                 'move_ids.move_dest_ids.location_dest_id', 'move_ids.move_dest_ids.state')
    def _compute_yaguven_hoja_consolidada(self):
        for picking in self:
            if picking.picking_type_id.yaguven_reabast_paso != 'recoleccion':
                picking.yaguven_hoja_consolidada = False
            else:
                picking.yaguven_hoja_consolidada = picking._build_hoja_consolidada()

    @staticmethod
    def _suc_label(transito):
        """Nombre corto de la sucursal a partir de su ubicación de tránsito 'Tránsito → <suc>'."""
        nombre = transito.name or transito.display_name or ''
        return nombre.split('→')[-1].strip() if '→' in nombre else nombre.strip()

    @staticmethod
    def _fmt_qty(q):
        return str(int(q)) if abs(q - round(q)) < 1e-6 else ('%g' % q)

    def _build_hoja_consolidada(self):
        """Arma la matriz producto × sucursal (HTML) desde los moves de la recolección y sus
        despachos encadenados. Valores dinámicos escapados (C.4)."""
        self.ensure_one()
        orden_suc = {}   # label -> índice de columna (orden de aparición)
        filas = []       # (producto_label, {suc_label: qty}, total)
        for mv in self.move_ids.filtered(lambda m: m.state != 'cancel'):
            celdas = {}
            for dmv in mv.move_dest_ids.filtered(lambda d: d.state != 'cancel'):
                label = self._suc_label(dmv.location_dest_id)
                celdas[label] = celdas.get(label, 0.0) + dmv.product_uom_qty
                orden_suc.setdefault(label, len(orden_suc))
            filas.append((mv.product_id.display_name, celdas, mv.product_uom_qty))

        if not filas:
            return Markup('<p class="text-muted">Sin líneas para mostrar.</p>')

        sucs = sorted(orden_suc, key=orden_suc.get)
        tot_col = {s: 0.0 for s in sucs}
        tot_gen = 0.0

        th = ''.join('<th class="text-end">%s</th>' % html_escape(s) for s in sucs)
        head = ('<thead><tr><th>Producto</th>%s'
                '<th class="text-end">Total a preparar</th></tr></thead>') % th

        body = ''
        for prod, celdas, total in filas:
            tds = ''
            for s in sucs:
                q = celdas.get(s)
                if q:
                    tot_col[s] += q
                    tds += '<td class="text-end">%s</td>' % html_escape(self._fmt_qty(q))
                else:
                    tds += '<td class="text-end text-muted">—</td>'
            tot_gen += total
            body += ('<tr><td>%s</td>%s<td class="text-end"><strong>%s</strong></td></tr>'
                     % (html_escape(prod), tds, html_escape(self._fmt_qty(total))))

        tds_tot = ''.join('<td class="text-end"><strong>%s</strong></td>'
                          % html_escape(self._fmt_qty(tot_col[s])) for s in sucs)
        foot = ('<tfoot><tr><td><strong>TOTAL</strong></td>%s'
                '<td class="text-end"><strong>%s</strong></td></tr></tfoot>'
                % (tds_tot, html_escape(self._fmt_qty(tot_gen))))

        return Markup('<table class="table table-sm table-bordered">%s<tbody>%s</tbody>%s</table>'
                      % (head, body, foot))
        for picking in self:
            if picking.picking_type_id.yaguven_reabast_paso != 'recoleccion':
                picking.yaguven_recoleccion_estado = False
            elif picking.state == 'done':
                picking.yaguven_recoleccion_estado = 'hecha'
            elif picking.yaguven_en_recoleccion:
                picking.yaguven_recoleccion_estado = 'en_recoleccion'
            else:
                picking.yaguven_recoleccion_estado = 'pendiente'

    # --- Despachos colgados: días parado (frente C, etapa 1) ---
    # Días que un despacho de reabastecimiento lleva abierto (ni hecho ni cancelado). Alimenta la
    # lista "Despachos colgados" (resaltado por antigüedad) y el aviso automático (etapa 2). No
    # almacenado: se recomputa al leer (basta para vista/dominio y para el cron diario). C.2.
    yaguven_dias_parado = fields.Integer(
        string='Días parado',
        compute='_compute_yaguven_dias_parado',
        help='Días que este despacho de reabastecimiento lleva sin terminarse ni cancelarse. '
             '0 si está hecho, cancelado o no es un despacho.')

    @api.depends('state', 'scheduled_date', 'picking_type_id.yaguven_reabast_paso')
    def _compute_yaguven_dias_parado(self):
        hoy = fields.Date.context_today(self)
        for picking in self:
            if (picking.picking_type_id.yaguven_reabast_paso == 'despacho'
                    and picking.state not in ('done', 'cancel') and picking.scheduled_date):
                # scheduled_date es Datetime en UTC -> pasar a la fecha local del usuario (ART) antes
                # de restar, para no correr un día por la diferencia horaria.
                sched = fields.Datetime.context_timestamp(
                    picking, fields.Datetime.to_datetime(picking.scheduled_date)).date()
                picking.yaguven_dias_parado = max((hoy - sched).days, 0)
            else:
                picking.yaguven_dias_parado = 0

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

    def action_cancel(self):
        """Ejercicio 2 de tensión funcional (2026-07-19): cancelar una recepción de
        reabastecimiento a mano (en vez de "Informar diferencias") deja el stock que ya viajó
        varado en la ubicación de Tránsito — el despacho que lo movió queda en 'done' (no se
        puede reabrir) y la recepción cancelada es un callejón sin salida; ningún informe lo
        expone salvo el Kardex.

        "Informar diferencias" (reabast_diferencia_wizard) ya reconcilia el Tránsito (fix
        2026-07-17): marcando cant_recibida=0 en todo, equivale a un faltante total y deja todo
        prolijo. Por eso una recepción de reabastecimiento no se cancela directo; se redirige ahí.
        """
        recepciones = self.filtered(
            lambda p: p.picking_type_id.yaguven_reabast_paso == 'recepcion'
            and p.state not in ('done', 'cancel'))
        if recepciones:
            raise UserError(_(
                "Esta recepción no se cancela directamente: dejaría el stock que ya salió de "
                "Central varado en Tránsito, sin ningún informe que lo muestre.\n\n"
                "Usá \"Informar diferencias\" y cargá 0 en la cantidad recibida de cada línea — "
                "reconcilia el Tránsito solo y le avisa a Central.\n\n"
                "Recepción: %s"
            ) % ', '.join(recepciones.mapped('name')))
        return super().action_cancel()
