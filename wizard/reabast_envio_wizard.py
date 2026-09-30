from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ReabastEnvioWizard(models.TransientModel):
    """«Enviar a otra sucursal»: una sucursal le manda mercadería a otra en un solo paso.

    Arma dos traslados encadenados: el ENVÍO (existencias de la que manda → tránsito de la que
    recibe), que valida la que manda, y la RECEPCIÓN (tránsito → existencias de la que recibe),
    con el tipo de Recepción de siempre. El gateo de button_validate no deja validar la recepción
    hasta que el envío esté hecho. Central no participa: sigue por recolección → despacho.
    """
    _name = 'yaguven.reabast.envio.wizard'
    _description = 'Enviar mercadería a otra sucursal'

    origen_id = fields.Many2one(
        'stock.warehouse', string='Sale de', required=True,
        default=lambda self: self._default_origen(),
        domain="[('id', 'in', origen_permitido_ids)]")
    # «Va a» es el TRÁNSITO de la sucursal que recibe, no su almacén: los almacenes tienen regla
    # por Unidad Operativa y una sucursal no puede leer el de otra (ni para mostrar su nombre). El
    # tránsito («Tránsito → Moreno») lo leen todos y de él sale el tipo de Recepción del destino.
    destino_id = fields.Many2one(
        'stock.location', string='Va a', required=True,
        domain="[('id', 'in', destino_permitido_ids)]")
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
            wiz.origen_permitido_ids = origenes
            wiz.destino_permitido_ids = recep.filtered(
                lambda t: t.warehouse_id.id != wiz.origen_id.id).default_location_src_id.ids

    @api.model
    def action_abrir(self):
        """Menú «Enviar a otra sucursal»."""
        if not self._origenes_del_usuario():
            raise UserError(_(
                "Tu usuario no tiene una sucursal asignada para enviar mercadería. "
                "Pedile a Central que te la asigne."))
        return {
            'type': 'ir.actions.act_window', 'name': _('Enviar a otra sucursal'),
            'res_model': self._name, 'view_mode': 'form', 'target': 'new',
        }

    def _tipo_recepcion(self, transito):
        recep = self._recepciones_envio().filtered(
            lambda t: t.default_location_src_id == transito)[:1]
        if not recep:
            raise UserError(_("«%s» no es el tránsito de una sucursal habilitada para envíos.")
                            % transito.display_name)
        return recep

    def action_enviar(self):
        self.ensure_one()
        lineas = self.line_ids.filtered(lambda l: l.product_uom_qty > 0)
        if not lineas:
            raise UserError(_("Cargá al menos un producto con cantidad."))
        if self.origen_id not in self._origenes_del_usuario():
            raise UserError(_("No podés enviar desde «%s».") % self.origen_id.display_name)
        recep_type = self._tipo_recepcion(self.destino_id)      # en sudo
        destino = recep_type.warehouse_id
        if destino.id == self.origen_id.id:
            raise UserError(_("La sucursal que recibe tiene que ser otra."))

        env_type = self.env['stock.picking.type']._yg_tipo_envio(self.origen_id)
        loc_origen = self.origen_id.lot_stock_id
        transito = self.destino_id
        loc_destino = recep_type.default_location_dest_id
        recep_type = recep_type.with_env(self.env)

        comp = self.origen_id.company_id
        Picking = self.env['stock.picking'].with_company(comp)
        Move = self.env['stock.move'].with_company(comp)
        origin = _('Envío %s → %s') % (self.origen_id.name.split('- ')[-1],
                                       destino.name.split('- ')[-1])

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
                'product_uom': ln.product_uom_id.id, 'company_id': comp.id,
                'location_id': loc_origen.id, 'location_dest_id': transito.id,
                'picking_id': envio.id, 'picking_type_id': env_type.id,
            })
            Move.create({
                'product_id': ln.product_id.id, 'product_uom_qty': ln.product_uom_qty,
                'product_uom': ln.product_uom_id.id, 'company_id': comp.id,
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

        return {
            'type': 'ir.actions.act_window', 'name': _('Envío'),
            'res_model': 'stock.picking', 'res_id': envio.id,
            'view_mode': 'form', 'target': 'current',
        }


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
        string='Disponible en la que manda', digits='Product Unit', compute='_compute_disponible')

    @api.depends('product_id')
    def _compute_uom(self):
        for ln in self:
            ln.product_uom_id = ln.product_id.uom_id

    @api.depends('product_id', 'wizard_id.origen_id')
    def _compute_disponible(self):
        for ln in self:
            loc = ln.wizard_id.origen_id.lot_stock_id
            ln.disponible = ln.product_id.with_context(location=loc.id).free_qty \
                if ln.product_id and loc else 0.0
