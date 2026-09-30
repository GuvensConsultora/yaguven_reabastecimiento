from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ReabastEnvioWizard(models.TransientModel):
    """«Enviar a otra sucursal» / «Pedir a otra sucursal»: mercadería entre sucursales.

    Modo ENVIAR: la que tiene la mercadería elige a dónde va. Modo PEDIR (1.24.0): la que la necesita
    elige a quién se la pide; el envío queda armado y pendiente para la otra, que lo valida cuando
    sale. En los dos modos `origen_id` es la sucursal del usuario («Sale de» / «Para»).

    Arma dos traslados encadenados: el ENVÍO (existencias de la que manda → tránsito de la que
    recibe), que valida la que manda, y la RECEPCIÓN (tránsito → existencias de la que recibe),
    con el tipo de Recepción de siempre. El gateo de button_validate no deja validar la recepción
    hasta que el envío esté hecho. Central no participa: sigue por recolección → despacho.
    """
    _name = 'yaguven.reabast.envio.wizard'
    _description = 'Enviar mercadería a otra sucursal'

    modo = fields.Selection([('enviar', 'Enviar'), ('pedir', 'Pedir')], default='enviar',
                            required=True, readonly=True)
    origen_id = fields.Many2one(
        'stock.warehouse', string='Sale de', required=True,
        default=lambda self: self._default_origen(),
        domain="[('id', 'in', origen_permitido_ids)]")
    # «Va a» es el TRÁNSITO de la sucursal que recibe, no su almacén: los almacenes tienen regla
    # por Unidad Operativa y una sucursal no puede leer el de otra (ni para mostrar su nombre). El
    # tránsito («Tránsito → Moreno») lo leen todos y de él sale el tipo de Recepción del destino.
    destino_id = fields.Many2one(
        'stock.location', string='Va a',
        domain="[('id', 'in', destino_permitido_ids)]")
    # Modo pedir: a quién se le pide, por su UNIDAD OPERATIVA («Padua»): el almacén no se puede
    # leer y sus existencias («A-Pad/Existencias») no se entienden ni se encuentran buscando «Padua».
    pide_a_id = fields.Many2one(
        'operating.unit', string='Le pide a',
        domain="[('id', 'in', pide_a_permitido_ids)]")
    pide_a_permitido_ids = fields.Many2many(
        'operating.unit', compute='_compute_permitidos')
    origen_permitido_ids = fields.Many2many(
        'stock.warehouse', compute='_compute_permitidos')
    destino_permitido_ids = fields.Many2many(
        'stock.location', compute='_compute_permitidos')
    line_ids = fields.One2many(
        'yaguven.reabast.envio.wizard.line', 'wizard_id', string='Productos')

    @api.model
    def _origenes_del_usuario(self):
        """La sucursal que manda sale de las Unidades Operativas del usuario (mismo criterio que
        «Pedir mercadería»); el Supervisor puede elegir cualquiera."""
        sucursales = self.env['stock.picking.type']._yg_sucursales_envio()
        if not self.env.user.has_group('yaguven_reabastecimiento.group_reabast_supervisor'):
            sucursales = sucursales.filtered(
                lambda w: w.operating_unit_id in self.env.user.operating_unit_ids)
        return sucursales.with_env(self.env)

    @api.model
    def _default_origen(self):
        origenes = self._origenes_del_usuario()
        return origenes[:1].id if len(origenes) == 1 else False

    @api.model
    def _recepciones_envio(self):
        """Tipos de Recepción de las sucursales habilitadas (en sudo: sólo para ids)."""
        return self.env['stock.picking.type'].sudo().search([
            ('yaguven_reabast_paso', '=', 'recepcion'),
            ('warehouse_id', 'in', self.env['stock.picking.type']._yg_sucursales_envio().ids)])

    @api.depends('origen_id')
    def _compute_permitidos(self):
        recep = self._recepciones_envio()
        origenes = self._origenes_del_usuario()
        for wiz in self:
            otras = recep.filtered(lambda t: t.warehouse_id.id != wiz.origen_id.id)
            wiz.origen_permitido_ids = origenes
            wiz.destino_permitido_ids = otras.default_location_src_id.ids
            wiz.pide_a_permitido_ids = otras.warehouse_id.operating_unit_id.ids

    def _sale_de_pedido(self):
        """Almacén (en sudo) de la Unidad Operativa a la que se le pide."""
        self.ensure_one()
        return self.env['stock.picking.type']._yg_sucursales_envio().filtered(
            lambda w: w.operating_unit_id == self.pide_a_id)[:1]

    def _loc_disponible(self):
        """Dónde se mira el stock libre de cada línea: en la sucursal de la que sale."""
        self.ensure_one()
        if self.modo == 'pedir':
            return self._sale_de_pedido().lot_stock_id.with_env(self.env)
        return self.origen_id.lot_stock_id

    @api.model
    def action_abrir(self, modo='enviar'):
        """Menús «Enviar a otra sucursal» y «Pedir a otra sucursal»."""
        if not self._origenes_del_usuario():
            raise UserError(_(
                "Tu usuario no tiene una sucursal asignada para mover mercadería. "
                "Pedile a Central que te la asigne."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pedir a otra sucursal') if modo == 'pedir' else _('Enviar a otra sucursal'),
            'res_model': self._name, 'view_mode': 'form', 'target': 'new',
            'context': {'default_modo': modo},
        }

    def _tipo_recepcion(self, transito):
        recep = self._recepciones_envio().filtered(
            lambda t: t.default_location_src_id == transito)[:1]
        if not recep:
            raise UserError(_("«%s» no es el tránsito de una sucursal habilitada para envíos.")
                            % transito.display_name)
        return recep

    def _lineas(self):
        lineas = self.line_ids.filtered(lambda l: l.product_uom_qty > 0)
        if not lineas:
            raise UserError(_("Cargá al menos un producto con cantidad."))
        if self.origen_id not in self._origenes_del_usuario():
            raise UserError(_("No podés operar por «%s».") % self.origen_id.display_name)
        return lineas

    def action_enviar(self):
        self.ensure_one()
        lineas = self._lineas()
        if not self.destino_id:
            raise UserError(_("Elegí a qué sucursal va."))
        recep_type = self._tipo_recepcion(self.destino_id)      # en sudo
        if recep_type.warehouse_id.id == self.origen_id.id:
            raise UserError(_("La sucursal que recibe tiene que ser otra."))
        envio = self._armar(self.origen_id, recep_type, lineas)
        return {
            'type': 'ir.actions.act_window', 'name': _('Envío'),
            'res_model': 'stock.picking', 'res_id': envio.id,
            'view_mode': 'form', 'target': 'current',
        }

    def action_pedir(self):
        """Modo pedir: el envío sale de `pide_a_id` hacia el tránsito de la sucursal del usuario y
        queda pendiente (reservado si hay stock) para que lo valide la que tiene la mercadería."""
        self.ensure_one()
        lineas = self._lineas()
        if not self.pide_a_id:
            raise UserError(_("Elegí a qué sucursal le pedís."))
        sale_de = self._sale_de_pedido()                          # en sudo
        if not sale_de or sale_de.id == self.origen_id.id:
            raise UserError(_("Elegí otra sucursal para pedirle."))
        recep_type = self._recepciones_envio().filtered(
            lambda t: t.warehouse_id.id == self.origen_id.id)[:1]
        if not recep_type:
            raise UserError(_("Tu sucursal no tiene tipo de Recepción de reabastecimiento."))
        envio = self._armar(sale_de, recep_type, lineas)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'sticky': False, 'title': _('Pedido hecho'),
                       'message': _('%s te lo manda con %s. Cuando salga, te aparece en Recepciones.')
                       % (sale_de.name.split('- ')[-1], envio.name),
                       'next': {'type': 'ir.actions.act_window_close'}},
        }

    def _armar(self, sale_de, recep_type, lineas):
        """Envío (existencias de `sale_de` → tránsito del destino) + recepción encadenada.
        `sale_de` y `recep_type` pueden venir en sudo (almacenes de otra UO): sólo se usan sus ids."""
        env_type = self.env['stock.picking.type']._yg_tipo_envio(sale_de)
        loc_origen = sale_de.lot_stock_id
        transito = recep_type.default_location_src_id
        loc_destino = recep_type.default_location_dest_id
        destino = recep_type.warehouse_id
        recep_type = recep_type.with_env(self.env)

        comp = sale_de.company_id
        Picking = self.env['stock.picking'].with_company(comp)
        Move = self.env['stock.move'].with_company(comp)
        origin = (_('Pedido %s → %s') if self.modo == 'pedir' else _('Envío %s → %s')) % (
            sale_de.name.split('- ')[-1], destino.name.split('- ')[-1])

        # El partner va desde el alta: yaguven_remito_sucursal lo escribe al confirmar, y en 19 un
        # cambio de partner recalcula el destino del traslado desde el tipo (_compute_location_id),
        # que en el envío es la propia sucursal y no el tránsito de la que recibe.
        envio = Picking.create({
            'picking_type_id': env_type.id, 'company_id': comp.id,
            'partner_id': destino.partner_id.id,
            'location_id': loc_origen.id, 'location_dest_id': transito.id, 'origin': origin,
        })
        recepcion = Picking.create({
            'picking_type_id': recep_type.id, 'company_id': comp.id,
            'location_id': transito.id, 'location_dest_id': loc_destino.id, 'origin': origin,
        })
        for ln in lineas:
            env_move = Move.create({
                'product_id': ln.product_id.id, 'product_uom_qty': ln.product_uom_qty,
                'uom_id': ln.product_uom_id.id, 'company_id': comp.id,
                'location_id': loc_origen.id, 'location_dest_id': transito.id,
                'picking_id': envio.id, 'picking_type_id': env_type.id,
            })
            Move.create({
                'product_id': ln.product_id.id, 'product_uom_qty': ln.product_uom_qty,
                'uom_id': ln.product_uom_id.id, 'company_id': comp.id,
                'location_id': transito.id, 'location_dest_id': loc_destino.id,
                'picking_id': recepcion.id, 'picking_type_id': recep_type.id,
                'procure_method': 'make_to_order',
                'move_orig_ids': [(4, env_move.id)],
            })
        (envio | recepcion).action_confirm()
        # si igual se recalculó (otro partner resuelto al confirmar), se vuelve al tránsito: el
        # write del picking lo propaga a los movimientos. Sin esto el envío deja el stock en el
        # tránsito pero su movimiento dice «Existencias» y la recepción pierde la cadena.
        if envio.location_dest_id != transito:
            envio.do_unreserve()
            envio.location_dest_id = transito
        envio.action_assign()
        return envio


class ReabastEnvioWizardLine(models.TransientModel):
    _name = 'yaguven.reabast.envio.wizard.line'
    _description = 'Producto a enviar a otra sucursal'

    wizard_id = fields.Many2one('yaguven.reabast.envio.wizard', required=True, ondelete='cascade')
    product_id = fields.Many2one(
        'product.product', string='Producto', required=True,
        domain="[('is_storable', '=', True)]")
    product_uom_qty = fields.Float(string='Cantidad', digits='Product Unit', default=1.0)
    product_uom_id = fields.Many2one(
        'uom.uom', string='Unidad', compute='_compute_uom', store=True, readonly=False)
    disponible = fields.Float(
        string='Disponible', digits='Product Unit', compute='_compute_disponible')

    @api.depends('product_id')
    def _compute_uom(self):
        for ln in self:
            ln.product_uom_id = ln.product_id.uom_id

    @api.depends('product_id', 'wizard_id.origen_id', 'wizard_id.pide_a_id', 'wizard_id.modo')
    def _compute_disponible(self):
        for ln in self:
            loc = ln.wizard_id._loc_disponible()
            ln.disponible = ln.product_id.with_context(location=loc.id).free_qty \
                if ln.product_id and loc else 0.0
