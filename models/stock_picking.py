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

    # --- Unidades operativas de la recolección (pedido de Anael 29/09) ---
    # Una recolección consolida pedidos de varias sucursales, así que la UO no es una sola: se
    # toma de los pedidos vinculados. La recolección parcial (backorder) no tiene pedidos propios
    # y hereda las de la original. Almacenado para poder filtrar y agrupar por UO en la lista.
    yaguven_reabast_pedido_ids = fields.One2many(
        'yaguven.reabast.pedido', 'picking_recoleccion_id', string='Pedidos de reabastecimiento')
    yaguven_reabast_uo_ids = fields.Many2many(
        'operating.unit', 'yaguven_reabast_picking_uo_rel', 'picking_id', 'operating_unit_id',
        string='Unidades operativas', compute='_compute_yaguven_reabast_uo_ids', store=True,
        help='Unidades operativas de las sucursales cuyos pedidos se preparan en esta recolección.')

    @api.depends('yaguven_reabast_pedido_ids.operating_unit_id',
                 'backorder_id.yaguven_reabast_uo_ids')
    def _compute_yaguven_reabast_uo_ids(self):
        for picking in self:
            picking.yaguven_reabast_uo_ids = (
                picking.yaguven_reabast_pedido_ids.operating_unit_id
                or picking.backorder_id.yaguven_reabast_uo_ids)

    # --- Envío entre sucursales: «Sale de mi sucursal» (filtro de la lista, 1.24.0) ---
    # El filtro corre en el navegador, donde no está `user.operating_unit_ids`: el campo resuelve
    # en el servidor, con las Unidades Operativas del usuario que busca.
    yaguven_sale_de_mi_uo = fields.Boolean(
        string='Sale de mi sucursal', compute='_compute_yaguven_sale_de_mi_uo',
        search='_search_yaguven_sale_de_mi_uo')

    def _compute_yaguven_sale_de_mi_uo(self):
        uos = self.env.user.operating_unit_ids
        for picking in self:
            picking.yaguven_sale_de_mi_uo = (
                picking.picking_type_id.sudo().warehouse_id.operating_unit_id in uos)

    def _search_yaguven_sale_de_mi_uo(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(_("Operación no soportada para «Sale de mi sucursal»."))
        tipos = self.env['stock.picking.type'].sudo().search([
            ('warehouse_id.operating_unit_id', 'in', self.env.user.operating_unit_ids.ids)])
        dentro = (operator == '=') == value
        return [('picking_type_id', 'in' if dentro else 'not in', tipos.ids)]

    # «De mi sucursal» (pedido de Anael 08/10): filtro de PANTALLA, no regla de acceso. Una regla
    # sobre stock.picking corta los circuitos que leen traslados de otra UO por dentro (despacho de
    # Central al validar la recepción, envío de la que manda al pedir); probado en testing el 09/10.
    # Quien tiene «ve todas las UO» (Central, supervisores) ve todo con el filtro puesto.
    yaguven_de_mi_sucursal = fields.Boolean(
        string='De mi sucursal', compute='_compute_yaguven_de_mi_sucursal',
        search='_search_yaguven_de_mi_sucursal')

    def _yaguven_tipos_de_mi_sucursal(self):
        """None = todas (perfil «ve todas las UO»); si no, los tipos de los almacenes de sus UO."""
        if self.env.user.has_group('yaguven_operating_unit.group_operating_unit_all'):
            return None
        return self.env['stock.picking.type'].sudo().search([
            ('warehouse_id.operating_unit_id', 'in', self.env.user.operating_unit_ids.ids)])

    def _compute_yaguven_de_mi_sucursal(self):
        tipos = self._yaguven_tipos_de_mi_sucursal()
        for picking in self:
            picking.yaguven_de_mi_sucursal = tipos is None or picking.picking_type_id in tipos

    def _search_yaguven_de_mi_sucursal(self, operator, value):
        if operator == 'in':
            operator, value = '=', True in value
        elif operator == 'not in':
            operator, value = '!=', True in value
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(_("Operación no soportada para «De mi sucursal»."))
        dentro = (operator == '=') == value
        tipos = self._yaguven_tipos_de_mi_sucursal()
        if tipos is None:
            return [] if dentro else [('id', '=', False)]
        return [('picking_type_id', 'in' if dentro else 'not in', tipos.ids)]

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
            # El envío entre sucursales lo valida la que manda (o el Supervisor): con «Pedir a otra
            # sucursal» lo arma la que recibe, y no puede darlo por salido ella misma. El almacén se
            # lee en sudo porque tiene regla por Unidad Operativa.
            if paso == 'envio' and not self.env.user.has_group(
                    'yaguven_reabastecimiento.group_reabast_supervisor'):
                uo = picking.picking_type_id.sudo().warehouse_id.operating_unit_id
                if uo not in self.env.user.operating_unit_ids:
                    raise UserError(_(
                        "Este envío lo valida la sucursal que manda (%s), cuando la mercadería sale.")
                        % uo.display_name)
            # pickings de origen = el/los paso(s) anterior(es) en la cadena make_to_order
            origen = picking.move_ids.move_orig_ids.picking_id.filtered(lambda p: p.id != picking.id)
            pendientes = origen.filtered(lambda p: p.state not in ('done', 'cancel'))
            if pendientes:
                # el paso anterior se nombra por lo que ES (una recepción puede venir de un
                # despacho de Central o de un envío entre sucursales), no por el paso actual
                anterior = {'recoleccion': 'la recolección',
                            'despacho': 'el despacho',
                            'envio': 'el envío'}.get(
                    pendientes[0].picking_type_id.yaguven_reabast_paso, 'el paso anterior')
                etiqueta = {'recoleccion': 'la recolección',
                            'despacho': 'el despacho',
                            'recepcion': 'la recepción'}.get(paso, 'este paso')
                raise UserError(_(
                    "No se puede validar %s todavía. Primero confirmá %s.\n\n"
                    "Pendiente: %s"
                ) % (etiqueta, anterior, ', '.join(pendientes.mapped('name'))))
        return super().button_validate()

    # ------------------------------------------------------------------
    # Faltante en la recolección (pedido de Anael 06/10): «Sin orden parcial» o «No se envía»
    # ------------------------------------------------------------------
    # Odoo reparte lo recolectado entre los despachos por orden de reserva. Lo que un despacho no
    # llega a reservar, con su origen ya cerrado, no va a viajar: se achica la demanda del despacho
    # y de la recepción de esa sucursal (si no, quedan esperando mercadería que nunca sale) y se le
    # avisa a la sucursal qué no le llega. Antes el faltante desaparecía sin rastro (RRAPAR00002).

    def _action_done(self):
        # `cancel_backorder` lo pone el nativo al validar con «Sin orden parcial»
        # (O20 stock_picking.py button_validate: pickings_not_to_backorder → _action_done).
        sin_parcial = self.browse()
        if self.env.context.get('cancel_backorder'):
            sin_parcial = self.filtered(
                lambda p: p.picking_type_id.yaguven_reabast_paso == 'recoleccion')
        recos = self.filtered(lambda p: p.picking_type_id.yaguven_reabast_paso == 'recoleccion')
        abiertos = sin_parcial._yaguven_despachos_abiertos()
        res = super()._action_done()
        if sin_parcial:
            sin_parcial._yaguven_ajustar_lo_que_no_viaja(
                _("La recolección %s se cerró sin dejar pendiente lo que faltaba."), abiertos)
        con_parcial = (recos - sin_parcial).filtered('backorder_ids')
        if con_parcial:
            con_parcial._yaguven_partir_tramos()
        return res

    def _yaguven_partir_tramos(self):
        """«Crear orden parcial» en la recolección: lo que cada sucursal no llegó a reservar pasa a
        un despacho y una recepción NUEVOS, colgados de la recolección pendiente. Así lo recolectado
        sale ya con su propio despacho/recepción (el control de pasos y «Informar diferencias»
        siguen igual) y lo que espera stock viaja después por su propio circuito. Mismo mecanismo
        que la orden parcial nativa (O20 stock.move._split + _create_backorder_picking)."""
        Move = self.env['stock.move']
        for reco in self:
            # un producto del que no se recolectó nada pasa ENTERO a la recolección pendiente:
            # sus despachos cuelgan de ella, no de la original
            despachos = (reco.move_ids | reco.backorder_ids.move_ids).move_dest_ids.filtered(
                lambda m: m.state not in ('done', 'cancel'))
            despachos._action_assign()
            nuevo_desp, nueva_recep = {}, {}   # picking original -> picking del segundo tramo
            for dmv in despachos:
                pendientes = dmv.move_orig_ids.filtered(lambda m: m.state not in ('done', 'cancel'))
                falta = dmv.product_uom_qty - dmv.quantity
                if not pendientes:
                    continue
                if dmv.uom_id.compare(falta, 0.0) <= 0:
                    # reservó todo: ya no espera nada de la recolección pendiente
                    dmv.move_orig_ids = [(3, m.id) for m in pendientes]
                    continue
                rmvs = dmv.move_dest_ids.filtered(lambda m: m.state not in ('done', 'cancel'))
                if dmv.uom_id.compare(dmv.quantity, 0.0) <= 0:
                    # no viaja nada ahora: la línea entera pasa al segundo tramo
                    d2, r2 = dmv, rmvs
                    dmv.move_orig_ids = [(6, 0, pendientes.ids)]
                else:
                    d2 = Move.create(dmv._split(falta))
                    d2._action_confirm(merge=False, create_proc=False)
                    r2 = Move
                    for rmv in rmvs:
                        r2 |= Move.create(rmv._split(falta))
                    r2._action_confirm(merge=False, create_proc=False)
                    # cada tramo con su origen: lo recolectado / lo que espera stock
                    dmv.write({'move_orig_ids': [(3, m.id) for m in pendientes],
                               'move_dest_ids': [(6, 0, rmvs.ids)]})
                    d2.write({'move_orig_ids': [(6, 0, pendientes.ids)],
                              'move_dest_ids': [(6, 0, r2.ids)]})
                    for rmv in rmvs:
                        rmv.move_orig_ids = [(6, 0, dmv.ids)]
                    r2.move_orig_ids = [(6, 0, d2.ids)]
                desp = dmv.picking_id
                if desp not in nuevo_desp:
                    nuevo_desp[desp] = desp._create_backorder_picking()
                d2.write({'picking_id': nuevo_desp[desp].id, 'picked': False})
                for rmv in r2:
                    recep = rmv.picking_id
                    if recep not in nueva_recep:
                        nueva_recep[recep] = recep._create_backorder_picking()
                    rmv.write({'picking_id': nueva_recep[recep].id, 'picked': False})
            (despachos | despachos.move_dest_ids)._recompute_state()

    def action_yaguven_no_se_envia(self):
        """Da de baja una recolección pendiente (lo que quedó esperando stock) con el mismo aviso
        a las sucursales que «Sin orden parcial». Sólo el Supervisor."""
        if not self.env.user.has_group('yaguven_reabastecimiento.group_reabast_supervisor'):
            raise UserError(_("Sólo el supervisor de Central puede dar de baja lo pendiente."))
        for picking in self:
            if picking.picking_type_id.yaguven_reabast_paso != 'recoleccion':
                raise UserError(_("«No se envía» aplica sólo a una recolección."))
            if picking.state in ('done', 'cancel'):
                raise UserError(_("La recolección %s ya está %s.") % (picking.name, picking.state))
            abiertos = picking._yaguven_despachos_abiertos()
            picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel'))._action_cancel()
            picking._yaguven_ajustar_lo_que_no_viaja(
                _("Central dio de baja lo que quedaba pendiente en la recolección %s."), abiertos)
        return True

    def _yaguven_despachos_abiertos(self):
        """Foto de los despachos abiertos ANTES de cerrar o dar de baja la recolección: si de un
        producto no se recolectó nada, Odoo cancela sus líneas de despacho y recepción solo, y sin
        esta foto la sucursal no se enteraría de lo que le falta. {move de despacho: demanda}."""
        # también la recepción de cada despacho: al cancelar, Odoo borra ese vínculo igual
        return {reco.id: {m.id: (m.product_uom_qty, m.move_dest_ids.ids)
                          for m in reco.move_ids.move_dest_ids
                          if m.state not in ('done', 'cancel')}
                for reco in self}

    def _yaguven_ajustar_lo_que_no_viaja(self, motivo, abiertos):
        """Sobre recolecciones ya cerradas (o dadas de baja): ajusta despacho y recepción de cada
        sucursal a lo que efectivamente viaja y le avisa lo que no."""
        Pedido = self.env['yaguven.reabast.pedido']
        for reco in self:
            no_viaja = {}   # recepción (picking) -> [(producto, pedido, viaja)]
            # Al cancelar, Odoo borra el vínculo recolección→despacho (O20 stock.move._action_cancel),
            # así que se parte de la foto tomada antes y no de move_dest_ids.
            foto = abiertos.get(reco.id, {})
            todos = self.env['stock.move'].browse(list(foto)).exists() | reco.move_ids.move_dest_ids
            # las que Odoo ya canceló solo (no se recolectó nada): viaja 0
            for dmv in todos.filtered(lambda m: m.id in foto and m.state == 'cancel'):
                pedido, recep_ids = foto[dmv.id]
                recep_moves = self.env['stock.move'].browse(recep_ids).exists().filtered(
                    lambda m: m.state != 'done')
                recep_moves.filtered(lambda m: m.state != 'cancel')._action_cancel()
                for recep in recep_moves.picking_id:
                    no_viaja.setdefault(recep, []).append((dmv.product_id, pedido, 0.0))
            despachos = todos.filtered(lambda m: m.state not in ('done', 'cancel'))
            despachos._action_assign()
            for dmv in despachos:
                # si todavía le puede llegar mercadería (otra recolección abierta), no se toca
                if dmv.move_orig_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
                    continue
                viaja = dmv.quantity
                if dmv.uom_id.compare(viaja, dmv.product_uom_qty) >= 0:
                    continue
                falta = dmv.product_uom_qty - viaja
                recep_moves = dmv.move_dest_ids.filtered(lambda m: m.state not in ('done', 'cancel'))
                for rmv in recep_moves:
                    no_viaja.setdefault(rmv.picking_id, []).append(
                        (dmv.product_id, dmv.product_uom_qty, viaja))
                if dmv.uom_id.compare(viaja, 0.0) <= 0:
                    dmv._action_cancel()
                    recep_moves._action_cancel()
                else:
                    dmv.product_uom_qty = viaja
                    for rmv in recep_moves:
                        rmv.product_uom_qty = max(rmv.product_uom_qty - falta, 0.0)

            raiz = reco
            while raiz.backorder_id:
                raiz = raiz.backorder_id
            for recep, filas in no_viaja.items():
                sucursal = recep.picking_type_id.warehouse_id
                pedidos = Pedido.search([('picking_recoleccion_id', '=', raiz.id),
                                         ('sucursal_id', '=', sucursal.id)])
                reco._yaguven_avisar_no_viaja(recep, pedidos, filas, motivo % reco.name)

    def _yaguven_avisar_no_viaja(self, recep, pedidos, filas, motivo):
        """Nota en la recepción y en los pedidos de la sucursal (C.4) + actividad a sus usuarios."""
        items = ''.join(
            '<li><strong>%s</strong>: pedido %s, viaja %s</li>' % (
                html_escape(prod.display_name), html_escape(self._fmt_qty(pedido)),
                html_escape(self._fmt_qty(viaja)))
            for prod, pedido, viaja in filas)
        body = Markup("<p><strong>%s</strong></p><p>No viaja a la sucursal:</p><ul>%s</ul>") % (
            motivo, Markup(items))
        for doc in [recep, *pedidos]:
            doc.message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_note")

        uo = recep.picking_type_id.warehouse_id.operating_unit_id
        recepcionistas = self.env.ref(
            'yaguven_reabastecimiento.group_reabast_recepcionista', raise_if_not_found=False)
        usuarios = (recepcionistas.all_user_ids if recepcionistas else self.env['res.users']).filtered(
            lambda u: uo in u.operating_unit_ids
            and not u.has_group('yaguven_reabastecimiento.group_reabast_supervisor'))
        for user in usuarios or pedidos.user_id:
            recep.activity_schedule(
                'mail.mail_activity_data_todo', user_id=user.id,
                summary=_("Mercadería que no viaja: %s") % recep.name, note=body)

    def action_cancel(self):
        """Ejercicio 2 de tensión funcional (2026-07-19): cancelar una recepción de
        reabastecimiento a mano (en vez de "Informar diferencias") deja el stock que ya viajó
        varado en la ubicación de Tránsito — el despacho que lo movió queda en 'done' (no se
        puede reabrir) y la recepción cancelada es un callejón sin salida; ningún informe lo
        expone salvo el Kardex.

        "Informar diferencias" (reabast_diferencia_wizard) ya reconcilia el Tránsito (fix
        2026-07-17): marcando cant_recibida=0 en todo, equivale a un faltante total y deja todo
        prolijo. Por eso una recepción de reabastecimiento no se cancela directo; se redirige ahí.

        Excepción (2026-07-20): cuando es el propio wizard el que cancela, DESPUÉS de reconciliar
        Tránsito y registrar la diferencia (caso "no llegó nada de nada" — Odoo no deja validar
        con cantidad total cero), se deja pasar. El flag de contexto solo lo pone ese wizard, no
        un cancel manual.
        """
        if self.env.context.get('yaguven_diferencia_wizard'):
            return super().action_cancel()
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
