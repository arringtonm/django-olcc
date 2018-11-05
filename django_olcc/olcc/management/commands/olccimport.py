import datetime
import os
import time
import xlrd
import csv
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from geopy import geocoders
from olcc.models import Product, ProductPrice, Store
from optparse import make_option

IMPORT_TYPES = ('csv_prices', 'prices', 'stores',)

class Command(BaseCommand):
    """
    This command parses an Excel spreadsheet containing OLCC product
    and price data and imports it into the database.
    """
    args = "<filename>"
    help = "Parses an excel document of OLCC price data."

    option_list = BaseCommand.option_list + (
        make_option('--quiet', action='store_true', dest='quiet',
            default=False, help='Suppress all output except errors'),
        make_option('--import-type', choices=IMPORT_TYPES,
            dest='import_type', default='prices',
            help='One of the following: %s' % (', '.join(IMPORT_TYPES),)),
        make_option('--geocode', action='store_true', dest='geocode',
            default=True, help='Geocode store addresses')
    )

    def uprint(self, msg):
        """
        Unbuffered print.
        """
        if not self.quiet:
            self.stdout.write("%s\n" % msg)
            self.stdout.flush()

    @transaction.commit_on_success
    def product_from_row(self, row):
        """
        Import a row of product price data as a new product record. The row data
        should correspond to a numeric price list file.

        :param row: A dict of keys mapped to row values.
        :return: A tuple containing a Product instance and a boolean indicating
                 if the Product record was newly created.
        """
        product = None
        created = False

        if Product.is_code_valid(row.get('code')):
            product, created = Product.objects.get_or_create(code=row.get('code'))

            if created:
                # Set the product title once and once only
                product.title = row.get('title')

            # Update our product
            if row.get('status'):
                product.status = row.get('status')
            if row.get('size'):
                product.size = row.get('size')
            if row.get('per_case'):
                product.bottles_per_case = int(float(row.get('per_case')))
            if row.get('proof'):
                product.proof = row.get('proof')
            if row.get('age'):
                # example values: '14 YRS', '1 YR', '8 MOS'
                m = re.search('(\\d+) (YRS?|MOS?)', row.get('age'))
                if m.group(2).startswith('MO'):
                    product.age = int(float(m.group(1))) / 12
                elif m.group(2).startswith('YR'):
                    product.age = m.group(1)

            # Persist our updates
            product.save()

            # Get the effective date for the new product price
            today = datetime.date.today()

            if row.get('price_effective_date'):
                price_date = datetime.datetime.strptime(
                        row.get('price_effective_date'), '%m/%d/%Y')
            elif row.get('year') and row.get('month'):
                price_date = datetime.date(int(float(row.get('year'))),
                    int(float(row.get('month'))), 1) 
            else:
                try:
                    next_month = today.replace(month=today.month+1, day=1)
                except ValueError:
                    if today.month == 12:
                        next_month = today.replace(year=today.year+1, month=1, day=1)

                # Effective date is next month
                price_date = next_month

            # Create the new price record
            try:
                # Strip any other values from the price like commas
                price = re.sub('[^0-9\.]', '', row.get('price'))

                ProductPrice.objects.create(amount=str(price),
                        effective_date=price_date, product=product)
            except IntegrityError:
                pass

        return (product, created)

    def import_csv_prices(self, csvreader):
        """
        Import a list of price and product data from the given CSV reader.
        """
        keys = ['code', 'status', 'title', 'size', 'age', 'proof', 'per_case',
                'price', 'price_effective_date']

        count = 0
        for row in csvreader:
            if len(row) == 0:
                continue

            # Strip any leading or trailing whitespace from the row values
            values = [str(s).strip() for s in row]

            # Map our keys to the row values
            obj = dict(zip(keys, values))

            # Import our product
            try:
                product, created = self.product_from_row(obj)

                if product:
                    count += 1
                    self.uprint("[%s]: %s" % (product.code, product.title))
            except Product.MultipleObjectsReturned:
                print "Product code '%s' returned multiple results!" % obj['code']

        self.uprint("\nImported '%s' new product records and/or prices!" % count)

        if count < 1:
            self.uprint("\nDid you specify the correct import type?")

    def import_prices(self, sheet):
        """
        Import a list of price and product data from the given
        sheet from an Excel workbook.
        """
        keys = ['code', 'status', 'title', 'size', 'age', 'proof', 'per_case',
                'price', 'price_effective_date']

        count = 0
        for n in range(sheet.nrows):
            values = sheet.row_values(n)

            # Strip any leading or trailing whitespace from the row values
            values = [str(s).strip() for s in values]

            # Map our keys to the row values
            obj = dict(zip(keys, values))

            # Import our product
            try:
                product, created = self.product_from_row(obj)

                if product:
                    count += 1
                    self.uprint("[%s]: %s" % (product.code, product.title))
            except Product.MultipleObjectsReturned:
                print "Product code '%s' returned multiple results!" % obj['code']

        self.uprint("\nImported '%s' new product records and/or prices!" % count)

        if count < 1:
            self.uprint("\nDid you specify the correct import type?")

    def import_stores(self, sheet):
        """
        Import a list of store data from the given sheet
        from an Excel workbook.
        """
        # Get our geocoder
        g = geocoders.Google()

        for n in range(sheet.nrows):
            values = sheet.row_values(n)

            store_key = values[0]
            if isinstance(store_key, (int, long, float)):
                # Create new store instance
                store = Store.from_row(values)

                if self.geocode:
                    try:
                        # Geocode the store location
                        address, pos = g.geocode(store.address_raw)

                        store.address = address.strip()
                        store.latitude = pos[0]
                        store.longitude = pos[1]
                        store.save()
                    except ValueError:
                        print "Multiple addresses returned for store %s!" % store.key

                    # Sleep to prevent hitting the geocoder rate limit
                    time.sleep(.35)

                # Some output
                self.uprint(store)

    def handle(self, *args, **options):
        self.quiet = options.get('quiet', False)
        self.geocode = options.get('geocode', True)
        self.import_type = options.get('import_type')

        try:
            # Get our filename
            filename = args[0]

            try:
                # Get our import method
                import_method = getattr(self, 'import_%s' % self.import_type)
            except AttributeError:
                raise CommandError("Import type '%s' not implemented!" % self.import_type)

            # Start the import
            self.uprint("Importing '%s' from: \n\t%s" % (self.import_type, filename))

            if self.import_type.startswith('csv'):
                with open(filename, 'rb') as csvfile:
                    import_method(csv.reader(csvfile))
            else:
                # Import workbook
                wb = xlrd.open_workbook(filename, on_demand=True)

                # Import the first sheet
                import_method(wb.sheet_by_index(0))
        except IndexError:
            raise CommandError("You must specify a filename!")
        except IOError, e:
            raise CommandError("No such file: '%s'" % e.filename)

