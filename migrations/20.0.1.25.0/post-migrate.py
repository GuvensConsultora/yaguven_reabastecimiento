"""Despachos de reabastecimiento: orden parcial SIEMPRE (pedido de Anael 06/10).

Si la recolección deja pendiente lo que faltaba («Crear orden parcial»), el despacho tiene que
esperarlo: con «Sin orden parcial» en el despacho, lo pendiente de la recolección llegaría a la
salida de Central sin un despacho que lo lleve (caso Cielo Raso, REABREC00006). La elección de
dejar o no pendiente se hace una sola vez, en la recolección, que sigue preguntando.

Los tipos de despacho no los crea el módulo (los arma la topología de cada sucursal), por eso se
ajustan acá y no en un data XML. Idempotente: re-correrlo no cambia nada.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {'active_test': False})
    tipos = env['stock.picking.type'].search([
        ('yaguven_reabast_paso', '=', 'despacho'),
        ('create_backorder', '!=', 'always'),
    ])
    tipos.write({'create_backorder': 'always'})
    _logger.info("yaguven_reabastecimiento: %s despachos pasan a orden parcial siempre: %s",
                 len(tipos), ', '.join(tipos.mapped('display_name')))
