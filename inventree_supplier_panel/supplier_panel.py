from django.http import HttpResponse
from django.http import JsonResponse
from django.urls import re_path, reverse

from order.models import PurchaseOrder
from part.models import Part
from plugin import InvenTreePlugin
from plugin.mixins import SettingsMixin, UrlsMixin, UserInterfaceMixin
from company.models import Company, ManufacturerPart, SupplierPart
from company.models import SupplierPriceBreak
from users.permissions import check_user_role
from common.models import InvenTreeSetting
from .version import PLUGIN_VERSION
from .mouser import Mouser
from .digikey import Digikey
from .farnell import Farnell
from .meta_access import MetaAccess
from .request_wrappers import Wrappers

import json
from datetime import datetime


class SupplierCartPanel(UserInterfaceMixin, SettingsMixin, InvenTreePlugin, UrlsMixin):

    PurchaseOrderPK = 0

    NAME = "SupplierCart"
    SLUG = "suppliercart"
    TITLE = "Create Shopping Cart"
    AUTHOR = "Michael"
    PUBLISH_DATE = "2026-08-17T00:00:00"
    DESCRIPTION = "This plugin allows to transfer a PO into a supplier shopping cart."
    VERSION = PLUGIN_VERSION
    COUNTRY_CODES = {'AUD': 'AU',
                     'CAD': 'CA',
                     'CNY': 'CN',
                     'GBP': 'GB',
                     'JPY': 'JP',
                     'NZD': 'NZ',
                     'USD': 'US',
                     'EUR': 'DE'
                     }

    SETTINGS = {
        'MOUSER_PK': {
            'name': 'Mouser Supplier ID',
            'description': 'Primary key of the Mouser supplier',
            'model': 'company.company',
        },
        'DIGIKEY_PK': {
            'name': 'Digikey Supplier ID',
            'description': 'Primary key of the Digikey supplier',
            'model': 'company.company',
        },
        'FARNELL_PK': {
            'name': 'Farnell Supplier ID',
            'description': 'Primary key of the Farnell supplier',
            'model': 'company.company',
        },
        'MOUSERCARTKEY': {
            'name': 'Mouser cart API key',
            'description': 'Place here your key for the Mouser shopping cart API',
        },
        'MOUSERSEARCHKEY': {
            'name': 'Mouser search API key',
            'description': 'Place here your key for the Mouser search API',
        },
        'MOUSERLANGUAGE': {
            'name': 'Mouser language',
            'description': 'The language that Mouser uses to answer your requests',
            'choices': [('English', 'Mouser answers in English'),
                        ('German', 'Mouser answers in German')],
            'default': 'German',
        },
        'FARNELLSEARCHKEY': {
            'name': 'Farnell search API key',
            'description': 'Place here your key for the Farnell search API',
        },
        'DIGIKEY_CLIENT_ID': {
            'name': 'Digikey ID',
            'description': 'Client ID for Digikey',
        },
        'DIGIKEY_CLIENT_SECRET': {
            'name': 'Digikey Secret',
            'description': 'Client secret for Digikey',
        },
        'DIGIKEY_TOKEN': {
            'name': 'Digikey token',
            'description': 'Token for Digikey',
        },
        'DIGIKEY_REFRESH_TOKEN': {
            'name': 'Digikey refresh token',
            'description': 'Digikey Refresh token',
        },
        'PROXY_CON': {
            'name': 'Proxy CON',
            'description': 'Connection protocol to proxy server if needed e.g. https',
        },
        'PROXY_URL': {
            'name': 'Proxy URL',
            'description': 'URL to proxy server if needed e.g. http://user:password@ipaddress:port',
        },
    }

# ----------------------------------------------------------------------------
# Here we check the settings and show som status messages. We also construct
# the Digikey redirect_uri that needs to put into the Digikey web page.
# If the pk of the supplier is not set ein tne settings, the supplier is
# disabled. The button for Digikey token creation is also here.

    def get_settings_content(self, request):
        client_id = self.get_setting('DIGIKEY_CLIENT_ID')
        base_url = InvenTreeSetting.get_setting('INVENTREE_BASE_URL')
        if base_url == '':
            base_url_state = '<span class="badge badge-left rounded-pill bg-danger">Missing</span>'
        elif base_url[0:5] != 'https':
            base_url_state = '<span class="badge badge-left rounded-pill bg-danger">Server does not run https</span>'
        else:
            base_url_state = '<span class="badge badge-left rounded-pill bg-success">OK</span>'
        redirect_uri = f'{base_url}/{self.base_url}digikeytoken/'
        url = f'https://api.digikey.com/v1/oauth2/authorize?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}'
        return f"""
        <p>Setup:</p>
        <ol>
        <li>Read the <a href="https://github.com/SergeoLacruz/inventree-supplier-panel"> docu </a> on github</li>
        <li>Enable the plugin</li>
        <li>Put all required keys into settings</li>
        <li>Enjoy</li>
        <li>Remove the shopping carts and lists regularly from your accounts</li>
        </ol>
        <p>Status:</p>
        <table class='table table-condensed'>
           <tr>
           <td>Server Base URL</td><td>{base_url_state}</td>
           </tr>
           <tr>
           <td>Callback URL (Add this to your Digikey account)</td><td>{redirect_uri}</td>
           </tr>
        </table>
        <a class="btn btn-dark" onclick="window.open('{url}','name','width=1000px,height=800px')"">
         Create Digikey Token
        </a>
        """

# ----------------------------------------------------------------------------
# Create custom panels for the new UserInterfaceMixin implementation.

    def _update_registered_suppliers(self):
        try:
            self.registered_suppliers['Mouser']['pk'] = int(self.get_setting('MOUSER_PK'))
            self.registered_suppliers['Mouser']['is_registered'] = True
        except Exception:
            self.registered_suppliers['Mouser']['is_registered'] = False
        try:
            self.registered_suppliers['Digikey']['pk'] = int(self.get_setting('DIGIKEY_PK'))
            self.registered_suppliers['Digikey']['is_registered'] = True
        except Exception:
            self.registered_suppliers['Digikey']['is_registered'] = False
        try:
            self.registered_suppliers['Farnell']['pk'] = int(self.get_setting('FARNELL_PK'))
            self.registered_suppliers['Farnell']['is_registered'] = True
        except Exception:
            self.registered_suppliers['Farnell']['is_registered'] = False

    def get_ui_panels(self, request, context, **kwargs):
        panels = []
        context = context or {}
        target_model = str(context.get('target_model', '')).lower()
        target_id = context.get('target_id')

        try:
            target_id = int(target_id)
        except Exception:
            target_id = None

        self._update_registered_suppliers()

        # For purchase orders: PO transfer
        if target_model == 'purchaseorder' and target_id:
            try:
                order = PurchaseOrder.objects.get(pk=target_id)
            except PurchaseOrder.DoesNotExist:
                order = None

            has_permission = (
                check_user_role(request.user, 'purchase_order', 'change')
                or check_user_role(request.user, 'purchase_order', 'delete')
                or check_user_role(request.user, 'purchase_order', 'add')
            )

            if order and has_permission:
                cart_data = MetaAccess.get_value(self, order, 'cart')

                for s in self.registered_suppliers:
                    supplier = self.registered_suppliers[s]
                    if supplier['is_registered'] and order.supplier.pk == supplier['pk']:
                        panels.append({
                            'key': f'{s.lower()}-actions-panel',
                            'title': supplier['name'] + ' Actions',
                            'icon': 'fa-cart-shopping',
                            'source': self.plugin_static_file('mouser_panel.js:renderSupplierCartPanel'),
                            'context': {
                                'order_id': order.pk,
                                'transfer_url': reverse('plugin:suppliercart:transfer-cart', kwargs={'pk': order.pk}),
                                'cart': cart_data,
                            },
                        })

        # For parts: Supplier part creation
        if target_model == 'part' and target_id:
            try:
                part = Part.objects.get(pk=target_id)
            except Part.DoesNotExist:
                part = None

            has_permission = (
                check_user_role(request.user, 'part', 'change')
                or check_user_role(request.user, 'part', 'delete')
                or check_user_role(request.user, 'part', 'add')
            )

            show_panel = any(s['is_registered'] for s in self.registered_suppliers.values())

            if part and has_permission and show_panel and part.purchaseable:
                manufacturer_parts = list(
                    ManufacturerPart.objects.filter(part=part.pk).values('pk', 'MPN')
                )

                supplier_list = [
                    {'pk': data['pk'], 'name': data['name']}
                    for data in self.registered_suppliers.values()
                    if data['is_registered']
                ]

                panels.append({
                    'key': 'add-supplier-part-panel',
                    'title': 'Automatic Supplier parts',
                    'icon': 'fa-cart-plus',
                    'source': self.plugin_static_file('add_supplierpart_panel.js:renderAddSupplierPartPanel'),
                    'context': {
                        'part_id': part.pk,
                        'suppliers': supplier_list,
                        'manufacturer_parts': manufacturer_parts,
                        'add_supplierpart_url': reverse('plugin:suppliercart:add-supplierpart'),
                    },
                })

        return panels

    def setup_urls(self):
        return [
            # This one is for the Digikey OAuth callback
            re_path(r'^digikeytoken/', self.receive_authcode, name='digikeytoken'),

            # Now for the plugin
            re_path(r'transfercart/(?P<pk>\d+)/', self.transfer_cart, name='transfer-cart'),
            re_path(r'addsupplierpart(?:\.(?P<format>json))?$', self.add_supplierpart, name='add-supplierpart'),
        ]

# --------------------------- get_partdata ------------------------------------
# This is just the wrapper that selects the proper supplier dependant function
    def get_partdata(self, supplier, sku, options):

        try:
            self.registered_suppliers['Mouser']['pk'] = int(self.get_setting('MOUSER_PK'))
        except Exception:
            pass
        try:
            self.registered_suppliers['Digikey']['pk'] = int(self.get_setting('DIGIKEY_PK'))
        except Exception:
            pass
        try:
            self.registered_suppliers['Farnell']['pk'] = int(self.get_setting('FARNELL_PK'))
        except Exception:
            pass

        part_data = {}
        for s in self.registered_suppliers:
            if supplier == self.registered_suppliers[s]['pk']:
                part_data = self.registered_suppliers[s]['get_partdata'](self, sku, options)
        return part_data

# --------------------------- receive_authcode --------------------------------
# This creates the Digikey token from the authcode

    def receive_authcode(self, request):
        auth_code = request.GET.get('code')
        url = 'https://api.digikey.com/v1/oauth2/token'
        redirect_uri = InvenTreeSetting.get_setting('INVENTREE_BASE_URL') + '/' + self.base_url + 'digikeytoken/'
        url_data = {
            'code': auth_code,
            'client_id': self.get_setting('DIGIKEY_CLIENT_ID'),
            'client_secret': self.get_setting('DIGIKEY_CLIENT_SECRET'),
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code'
        }
        header = {}
        response = Wrappers.post_request(self, url_data, url, headers=header)
        if response.status_code == 200:
            print('\033[32mAccess Token get SUCCESS\033[0m')
            response_data = response.json()
            self.set_setting('DIGIKEY_TOKEN', response_data['access_token'])
            self.set_setting('DIGIKEY_REFRESH_TOKEN', response_data['refresh_token'])
            return HttpResponse('New Digikey token successfully received')
        else:
            print('\033[31m\033[1mReceive access token FAILED\033[0m')
            return HttpResponse(response.content)

# --------------------------- transfer_cart ------------------------------------
# This is called when the button is pressed and does most of the work.

    def transfer_cart(self, request, pk):

        self.PurchaseOrderPK = int(pk)
        order = PurchaseOrder.objects.filter(id=pk).all()[0]
        supplier = None
        try:
            self.registered_suppliers['Mouser']['pk'] = int(self.get_setting('MOUSER_PK'))
        except Exception:
            pass
        try:
            self.registered_suppliers['Digikey']['pk'] = int(self.get_setting('DIGIKEY_PK'))
        except Exception:
            pass
        for s in self.registered_suppliers:
            if order.supplier.pk == self.registered_suppliers[s]['pk']:
                supplier = s

        if supplier is None:
            return JsonResponse({'error_status': 'Supplier not configured', 'message': 'Supplier not configured'})

        # First create the shopping cart
        cart_data = self.registered_suppliers[supplier]['create_cart'](self, order)
        if cart_data['error_status'] != 'OK':
            cart_data['message'] = cart_data['error_status']
            return JsonResponse(cart_data)

        # Then fill it
        cart_data = self.registered_suppliers[supplier]['update_cart'](self, order, cart_data['ID'])
        if cart_data['error_status'] != 'OK':
            cart_data['message'] = cart_data['error_status']
            return JsonResponse(cart_data)

        # Now we transfer the actual prices back into the PO
        for po_item in order.lines.all():
            for item in cart_data['CartItems']:
                if po_item.part.SKU == item['SKU']:
                    po_item.purchase_price = item['UnitPrice']
                    po_item.save()
        cart_data['message'] = 'OK'
        cart_data['pk'] = pk
        cart_data['cart_date'] = datetime.today().strftime('%Y-%m-%d')
        MetaAccess.set_value(self, order, 'cart', cart_data)
        return JsonResponse(cart_data)

# ---------------------------- add_supplierpart -------------------------------
    def add_supplierpart(self, request):
        rdata = json.loads(request.body)
        part = Part.objects.filter(id=rdata['pk'])[0]
        supplier = Company.objects.filter(id=rdata['supplier'])[0]
        rdata['sku'] = rdata['sku'].strip()
        if (rdata['sku'] == ''):
            return JsonResponse({"message": "Please provide part number"})
        manufacturer_part = ManufacturerPart.objects.filter(id=rdata['mpart'])[0]
        supplier_parts = SupplierPart.objects.filter(part=rdata['pk'])
        for sp in supplier_parts:
            if sp.SKU.strip() == rdata['sku']:
                return JsonResponse({"message": "Supplierpart with this SKU already exists"})

        # Here start the new interface
        data = self.get_partdata(rdata['supplier'], rdata['sku'], 'exact')
        if data['error_status'] != 'OK':
            return JsonResponse({"message": data['error_status']})
        if data['number_of_results'] == 0:
            return JsonResponse({"message": "Part not found"})
        if (data['MPN'] != manufacturer_part.MPN) and not rdata['ignoreMPNCheck']:
            return JsonResponse({"message": "MPN does not match. " + data['MPN'] + " != " + manufacturer_part.MPN})
        sp = SupplierPart.objects.create(part=part,
                                         supplier=supplier,
                                         manufacturer_part=manufacturer_part,
                                         SKU=data['SKU'],
                                         link=data['URL'],
                                         note=data['lifecycle_status'],
                                         packaging=data['package'],
                                         pack_quantity=data['pack_quantity'],
                                         description=data['description'],
                                         )
        for pb in data['price_breaks']:
            SupplierPriceBreak.objects.create(part=sp, quantity=pb['Quantity'], price=pb['Price'], price_currency=pb['Currency'])
        return JsonResponse({"message": "OK"})

# ---------------------------- Define the suppliers ----------------------------
    registered_suppliers = {'Mouser': {'pk': 0,
                                       'name': 'Mouser',
                                       'po_template': 'supplier_panel/mouser.html',
                                       'is_registered': False,
                                       'get_partdata': Mouser.get_mouser_partdata,
                                       'update_cart': Mouser.update_mouser_cart,
                                       'create_cart': Mouser.create_mouser_cart,
                                       },
                            'Digikey': {'pk': 0,
                                        'name': 'Digikey',
                                        'po_template': 'supplier_panel/mouser.html',
                                        'is_registered': False,
                                        'get_partdata': Digikey.get_digikey_partdata_v4,
                                        'update_cart': Digikey.update_digikey_cart,
                                        'create_cart': Digikey.create_digikey_cart,
                                        },
                            'Farnell': {'pk': 0,
                                        'name': 'Farnell',
                                        'po_template': 'supplier_panel/mouser.html',
                                        'is_registered': False,
                                        'get_partdata': Farnell.get_farnell_partdata,
                                        'update_cart': '',
                                        'create_cart': Farnell.create_farnell_cart,
                                        }
                            }
