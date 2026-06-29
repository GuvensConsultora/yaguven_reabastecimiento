{
    'name': 'Yagüven - Reabastecimiento Central/Sucursales',
    'version': '19.0.1.0.0',
    'summary': 'Circuito de reabastecimiento con gateo de pasos (recolección → despacho → recepción)',
    'description': """
Capa operativa del circuito de reabastecimiento Central → Sucursales (y sucursal → sucursal).

Sub-ladrillo 2b (gateo): cada paso del circuito (recolección, despacho, recepción) solo puede
validarse cuando el paso anterior de la cadena está hecho, con un mensaje claro en castellano
en vez del error nativo críptico ("no se puede validar sin reservas").
""",
    'author': 'Yagüven C.G.',
    'website': 'https://yaguven.com',
    'category': 'Inventory',
    'depends': ['stock'],
    'data': [],
    'license': 'LGPL-3',
    'installable': True,
}
