{
    'name': 'Yagüven - Reabastecimiento Central/Sucursales',
    'version': '19.0.1.4.0',
    'summary': 'Circuito de reabastecimiento con gateo de pasos (recolección → despacho → recepción)',
    'description': """
Capa operativa del circuito de reabastecimiento Central → Sucursales (y sucursal → sucursal).

Sub-ladrillo 2b (gateo): cada paso del circuito (recolección, despacho, recepción) solo puede
validarse cuando el paso anterior de la cadena está hecho, con un mensaje claro en castellano
en vez del error nativo críptico ("no se puede validar sin reservas").

Sub-ladrillo 2c (bandejas por rol): grupos de seguridad Recolector / Despachador / Recepcionista
(+ Supervisor) y un menú "Reabastecimiento" donde cada rol ve solo sus pasos (recolecciones /
despachos / recepciones), filtrados por picking_type_id.yaguven_reabast_paso.

Sub-ladrillo 2d (estado "En recolección" + cutoff): la recolección tiene ciclo Pendiente → En
recolección (congelada) → Hecha. Al "Comenzar recolección" el documento se congela y los pedidos
nuevos arman una recolección nueva en vez de fusionarse (cutoff vía
stock.move._search_picking_for_assignation_domain).

Sub-ladrillo 3a (Pedido de reabastecimiento): documento liviano (yaguven.reabast.pedido) donde una
sucursal -o Central por ella- carga lo que necesita; al enviarlo, "Armar recolección" lo consolida
con los orderpoints. Scoping por Unidad Operativa (yaguven_operating_unit): cada sucursal ve solo
sus pedidos, Central ve todos.

Sub-ladrillo 3b (Armar recolección): wizard de confirmación (Supervisor) que consolida los pedidos
enviados en UNA recolección (un move por producto, cantidad total) + un despacho por sucursal
encadenado (make_to_order), marca los pedidos "Procesado" y abre la recolección.
""",
    'author': 'Yagüven C.G.',
    'website': 'https://yaguven.com',
    'category': 'Inventory',
    # mail -> chatter/tracking del pedido; yaguven_operating_unit -> datasource del scoping por UO
    # (operating.unit, res.users.operating_unit_ids, stock.warehouse.operating_unit_id). No se HEREDA
    # de ese módulo (C.2): se usa como datasource vía campo related + regla de registro.
    'depends': ['stock', 'mail', 'yaguven_operating_unit'],
    'data': [
        'security/reabast_groups.xml',
        'security/ir.model.access.csv',
        'security/reabast_pedido_rules.xml',
        'data/reabast_pedido_sequence.xml',
        'views/reabast_menus.xml',
        'views/reabast_picking_views.xml',
        'views/reabast_pedido_views.xml',
        'views/reabast_armar_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
}
