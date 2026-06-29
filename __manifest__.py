{
    'name': 'Yagüven - Reabastecimiento Central/Sucursales',
    'version': '19.0.1.2.0',
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
""",
    'author': 'Yagüven C.G.',
    'website': 'https://yaguven.com',
    'category': 'Inventory',
    'depends': ['stock'],
    'data': [
        'security/reabast_groups.xml',
        'views/reabast_menus.xml',
        'views/reabast_picking_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
}
