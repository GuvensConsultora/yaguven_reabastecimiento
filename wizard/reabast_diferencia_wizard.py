from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero
from odoo.tools.misc import html_escape


class ReabastDiferenciaWizard(models.TransientModel):
    """5a — Informar diferencias de una recepción de reabastecimiento (rol Recepcionista).

    Precarga lo esperado (moves de la recepción), toma lo recibido real, valida la recepción con
    lo físico (sin backorder: el faltante NO queda como recepción colgada, queda como diferencia a
    resolver por Central), registra la diferencia y le abre a Central una actividad To-Do.
    """
    _name = 'yaguven.reabast.diferencia.wizard'
    _description = 'Informar diferencias de recepción'

    picking_id = fields.Many2one('stock.picking', string='Recepción', required=True, readonly=True)
    line_ids = fields.One2many(
        'yaguven.reabast.diferencia.wizard.line', 'wizard_id', string='Lo esperado')
    extra_ids = fields.One2many(
        'yaguven.reabast.diferencia.wizard.extra', 'wizard_id', string='Sobrantes / incorrectos',
        help='Productos que llegaron y NO estaban en el remito: sobrante (de más) o incorrecto '
             '(vino en lugar de otro).')

    # ------------------------------------------------------------------
    # Construcción inicial: una línea por move de la recepción (esperado)
    # ------------------------------------------------------------------
    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        picking_id = self.env.context.get('active_id') or res.get('picking_id')
        if not picking_id:
            return res
        picking = self.env['stock.picking'].browse(picking_id)
        if picking.picking_type_id.yaguven_reabast_paso != 'recepcion':
            raise UserError(_("Las diferencias se informan sobre una recepción de reabastecimiento."))
        if picking.state in ('done', 'cancel'):
            raise UserError(_("Esta recepción ya está %s: no admite informar diferencias.") % picking.state)
        res['picking_id'] = picking.id
        vals = []
        for mv in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
            vals.append((0, 0, {
                'move_id': mv.id,
                'producto_id': mv.product_id.id,
                'cant_esperada': mv.product_uom_qty,
                'cant_recibida': mv.product_uom_qty,   # default: llegó todo (se edita lo que difiera)
            }))
        res['line_ids'] = vals
        return res

    # ------------------------------------------------------------------
    # Aplicar: validar la recepción con lo físico + registrar la diferencia + avisar a Central
    # ------------------------------------------------------------------
    def action_aplicar(self):
        self.ensure_one()
        picking = self.picking_id
        if picking.state in ('done', 'cancel'):
            raise UserError(_("Esta recepción ya está %s.") % picking.state)

        company = picking.company_id
        sucursal = picking.picking_type_id.warehouse_id

        dif_lines = []   # (0,0,vals) para yaguven.reabast.diferencia.linea

        # 1) Esperado: escribir la cantidad recibida en cada move + registrar faltante/sobrante/cantidad
        for ln in self.line_ids:
            mv = ln.move_id
            if not mv:
                continue
            prod = ln.producto_id
            rounding = prod.uom_id.rounding or 1.0
            if float_compare(ln.cant_recibida, 0.0, precision_rounding=rounding) < 0:
                raise UserError(_("La cantidad recibida no puede ser negativa (%s).") % prod.display_name)
            mv.with_company(company).write({'quantity': ln.cant_recibida, 'picked': True})

            delta = float_compare(ln.cant_recibida, ln.cant_esperada, precision_rounding=rounding)
            if delta != 0:
                falto = ln.cant_esperada - ln.cant_recibida
                # tipo: faltante si llegó 0, si no "cantidad distinta" (incluye recibir de más del mismo prod)
                tipo = 'faltante' if float_is_zero(ln.cant_recibida, precision_rounding=rounding) else 'cantidad'
                dif_lines.append((0, 0, {
                    'tipo': tipo,
                    'producto_esperado_id': prod.id,
                    'producto_recibido_id': prod.id if ln.cant_recibida else False,
                    'cant_esperada': ln.cant_esperada,
                    'cant_recibida': ln.cant_recibida,
                    'nota': ln.nota or False,
                }))

        # 2) Extras: productos que llegaron y no estaban (sobrante / incorrecto). Se REGISTRAN; el
        #    efecto sobre el stock (entra a la sucursal / devolución) lo decide Central en 5b.
        for ex in self.extra_ids:
            prod = ex.producto_id
            rounding = prod.uom_id.rounding or 1.0
            if float_compare(ex.cant_recibida, 0.0, precision_rounding=rounding) <= 0:
                raise UserError(_("Cargá una cantidad recibida mayor a cero en el extra %s.") % prod.display_name)
            dif_lines.append((0, 0, {
                'tipo': ex.tipo,
                'producto_esperado_id': False,
                'producto_recibido_id': prod.id,
                'cant_esperada': 0.0,
                'cant_recibida': ex.cant_recibida,
                'nota': ex.nota or False,
            }))

        # 3) Validar la recepción con lo físico, SIN backorder (el faltante no queda como recepción
        #    colgada; se resuelve como diferencia). skip_backorder + picking_ids_not_to_backorder
        #    son los contextos nativos que saltan el wizard de backorder y no lo crean.
        picking.with_company(company).with_context(
            skip_backorder=True, picking_ids_not_to_backorder=picking.ids,
        ).button_validate()

        # 4) Registrar la diferencia (si hubo alguna) y avisar a Central
        if not dif_lines:
            picking.message_post(
                body=Markup(_("<p><strong>Recepción sin diferencias.</strong></p>"
                              "<p>La sucursal confirmó que llegó todo lo despachado.</p>")),
                message_type="comment", subtype_xmlid="mail.mt_note")
            return {'type': 'ir.actions.act_window_close'}

        diferencia = self.env['yaguven.reabast.diferencia'].create({
            'picking_id': picking.id,
            'sucursal_id': sucursal.id if sucursal else False,
            'line_ids': dif_lines,
        })
        picking.yaguven_diferencia_id = diferencia.id
        self._notificar_central(diferencia)

        # abrir la diferencia recién creada para que el recepcionista la vea
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'yaguven.reabast.diferencia',
            'res_id': diferencia.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _notificar_central(self, diferencia):
        """Mensaje en la diferencia (C.4) + actividad To-Do para el/los Supervisor(es) de Central."""
        filas = ''.join(
            '<li>%s — <strong>%s</strong> (esperado %s / recibido %s)%s</li>' % (
                html_escape(dict(l._fields['tipo'].selection).get(l.tipo, l.tipo)),
                html_escape((l.producto_recibido_id or l.producto_esperado_id).display_name),
                html_escape('%g' % l.cant_esperada), html_escape('%g' % l.cant_recibida),
                (' — %s' % html_escape(l.nota)) if l.nota else '',
            ) for l in diferencia.line_ids)
        suc = diferencia.sucursal_id.display_name or _('sucursal')
        body = (_("<p><strong>Diferencias informadas por %s.</strong></p>"
                  "<p>La sucursal recibió con diferencias respecto al remito. Central debe resolver:</p>"
                  "<ul>%s</ul>") % (html_escape(suc), filas))
        diferencia.message_post(body=Markup(body), message_type="comment",
                                subtype_xmlid="mail.mt_note")

        supervisores = self.env.ref(
            'yaguven_reabastecimiento.group_reabast_supervisor', raise_if_not_found=False)
        resumen = _("Resolver diferencias de recepción %s (sucursal %s)") % (
            diferencia.picking_id.name, suc)
        # O19: res.groups ya no tiene 'users' -> all_user_ids (miembros directos + implicados).
        for user in (supervisores.all_user_ids if supervisores else self.env['res.users']):
            diferencia.activity_schedule(
                'mail.mail_activity_data_todo', user_id=user.id, summary=resumen)


class ReabastDiferenciaWizardLine(models.TransientModel):
    _name = 'yaguven.reabast.diferencia.wizard.line'
    _description = 'Línea esperada al informar diferencias'
    _order = 'producto_id, id'

    wizard_id = fields.Many2one('yaguven.reabast.diferencia.wizard', required=True, ondelete='cascade')
    move_id = fields.Many2one('stock.move', string='Movimiento', readonly=True)
    producto_id = fields.Many2one('product.product', string='Producto', readonly=True)
    cant_esperada = fields.Float(string='Esperado', readonly=True)
    cant_recibida = fields.Float(string='Recibido')
    nota = fields.Char(string='Nota')
    faltante = fields.Float(string='Faltante', compute='_compute_faltante')

    @api.depends('cant_esperada', 'cant_recibida')
    def _compute_faltante(self):
        for ln in self:
            ln.faltante = ln.cant_esperada - ln.cant_recibida


class ReabastDiferenciaWizardExtra(models.TransientModel):
    _name = 'yaguven.reabast.diferencia.wizard.extra'
    _description = 'Sobrante / producto incorrecto al informar diferencias'
    _order = 'id'

    wizard_id = fields.Many2one('yaguven.reabast.diferencia.wizard', required=True, ondelete='cascade')
    producto_id = fields.Many2one('product.product', string='Producto recibido', required=True)
    tipo = fields.Selection(
        [('sobrante', 'Sobrante (de más)'),
         ('incorrecto', 'Incorrecto (en lugar de otro)')],
        string='Tipo', default='sobrante', required=True)
    cant_recibida = fields.Float(string='Cantidad recibida')
    nota = fields.Char(string='Nota', help='Ej. "vino en lugar de Lija grano 100".')
