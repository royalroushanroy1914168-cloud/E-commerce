from datetime import datetime
from functools import wraps
from io import BytesIO
import os
import re
import sqlite3
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from fpdf import FPDF
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'roymart_super_secret_key')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'shop.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'images')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ORDER_STATUSES = [
    'Order Placed',
    'Confirmed',
    'Processing',
    'Shipped',
    'Out for Delivery',
    'Delivered',
    'Cancelled',
]

PAYMENT_STATUSES = [
    'Pending Verification',
    'Verified',
    'COD',
    'Refund Pending Seller Proof',
    'Refunded',
]

COMPLAINT_TYPES = [
    'Product Issue Complaint',
    'Refund Complaint',
    'Other',
]

COMPLAINT_STATUSES = [
    'In Help Desk',
    'Notice Sent to Seller',
    'Escalated to Admin',
    'Awaiting Seller Refund Proof',
    'Refund Proof Submitted',
    'Refund Verified',
    'Resolved',
    'Closed',
]

AUTO_DELETE_CLOSED_DAYS = 6
INVOICE_ELIGIBLE_STATUSES = ('Shipped', 'Out for Delivery', 'Delivered')


# =========================================
# 1. Database Setup, Migration & Auto-Cleanup
# =========================================
def get_db():
  conn = sqlite3.connect(DATABASE)
  conn.row_factory = sqlite3.Row
  return conn


def cleanup_expired_closed_complaints(conn):
  """Automatically deletes any closed complaint 6 days after its closure timestamp."""
  conn.execute(
      """DELETE FROM complaints
           WHERE status = 'Closed'
             AND closed_at IS NOT NULL
             AND (julianday('now') - julianday(closed_at)) >= ?""",
      (AUTO_DELETE_CLOSED_DAYS,),
  )
  conn.commit()


def init_db():
  conn = get_db()
  cursor = conn.cursor()

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'customer',
            qr_code TEXT
        )
    """)
  user_cols = [
      row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()
  ]
  if 'qr_code' not in user_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN qr_code TEXT")

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL,
            stock_qty INTEGER DEFAULT 10,
            description TEXT,
            image TEXT DEFAULT 'placeholder.png',
            FOREIGN KEY (seller_id) REFERENCES users (id)
        )
    """)
  prod_cols = [
      row[1] for row in cursor.execute("PRAGMA table_info(products)").fetchall()
  ]
  if 'seller_id' not in prod_cols:
    cursor.execute("ALTER TABLE products ADD COLUMN seller_id INTEGER")
  if 'stock_qty' not in prod_cols:
    cursor.execute("ALTER TABLE products ADD COLUMN stock_qty INTEGER DEFAULT 10")

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            user_id INTEGER,
            reviewer_name TEXT NOT NULL,
            rating INTEGER NOT NULL,
            comment TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products (id)
        )
    """)

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            address TEXT NOT NULL,
            phone TEXT NOT NULL,
            total_price REAL NOT NULL,
            payment_method TEXT DEFAULT 'Cash on Delivery',
            transaction_id TEXT,
            payment_status TEXT DEFAULT 'COD',
            status TEXT DEFAULT 'Order Placed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
  order_cols = [
      row[1] for row in cursor.execute("PRAGMA table_info(orders)").fetchall()
  ]
  if 'payment_method' not in order_cols:
    cursor.execute(
        "ALTER TABLE orders ADD COLUMN payment_method TEXT DEFAULT 'Cash on"
        " Delivery'"
    )
  if 'transaction_id' not in order_cols:
    cursor.execute("ALTER TABLE orders ADD COLUMN transaction_id TEXT")
  if 'payment_status' not in order_cols:
    cursor.execute(
        "ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT 'COD'"
    )

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            seller_id INTEGER,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        )
    """)
  item_cols = [
      row[1]
      for row in cursor.execute("PRAGMA table_info(order_items)").fetchall()
  ]
  if 'product_id' not in item_cols:
    cursor.execute("ALTER TABLE order_items ADD COLUMN product_id INTEGER")
  if 'seller_id' not in item_cols:
    cursor.execute("ALTER TABLE order_items ADD COLUMN seller_id INTEGER")

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS seller_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER,
            seller_identifier TEXT NOT NULL,
            sent_by TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

  cursor.execute("""
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            order_id INTEGER,
            product_id INTEGER,
            seller_id INTEGER,
            complaint_type TEXT DEFAULT 'Product Issue Complaint',
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            help_center_access INTEGER DEFAULT 1,
            escalated_to_admin INTEGER DEFAULT 0,
            escalation_note TEXT,
            seller_notice TEXT,
            help_reply TEXT,
            admin_solution TEXT,
            refund_approved INTEGER DEFAULT 0,
            seller_refund_txn_id TEXT,
            refund_verified INTEGER DEFAULT 0,
            status TEXT DEFAULT 'In Help Desk',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP
        )
    """)

  comp_cols = [
      row[1]
      for row in cursor.execute("PRAGMA table_info(complaints)").fetchall()
  ]
  if 'complaint_type' not in comp_cols:
    cursor.execute(
        "ALTER TABLE complaints ADD COLUMN complaint_type TEXT DEFAULT"
        " 'Product Issue Complaint'"
    )
  if 'escalated_to_admin' not in comp_cols:
    cursor.execute(
        "ALTER TABLE complaints ADD COLUMN escalated_to_admin INTEGER DEFAULT 0"
    )
  if 'escalation_note' not in comp_cols:
    cursor.execute("ALTER TABLE complaints ADD COLUMN escalation_note TEXT")
  if 'admin_solution' not in comp_cols:
    cursor.execute("ALTER TABLE complaints ADD COLUMN admin_solution TEXT")
  if 'refund_approved' not in comp_cols:
    cursor.execute(
        "ALTER TABLE complaints ADD COLUMN refund_approved INTEGER DEFAULT 0"
    )
  if 'seller_refund_txn_id' not in comp_cols:
    cursor.execute("ALTER TABLE complaints ADD COLUMN seller_refund_txn_id TEXT")
  if 'refund_verified' not in comp_cols:
    cursor.execute(
        "ALTER TABLE complaints ADD COLUMN refund_verified INTEGER DEFAULT 0"
    )
  if 'closed_at' not in comp_cols:
    cursor.execute("ALTER TABLE complaints ADD COLUMN closed_at TIMESTAMP")

  # Master Admin account (sell1 / sell1)
  cursor.execute("SELECT id FROM users WHERE email = ?", ('sell1',))
  master_row = cursor.fetchone()
  if not master_row:
    cursor.execute(
        "INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)",
        ('Master Admin (sell1)', 'sell1', generate_password_hash('sell1'), 'seller'),
    )
    master_id = cursor.lastrowid
  else:
    master_id = master_row['id']

  cursor.execute("SELECT COUNT(*) FROM products")
  if cursor.fetchone()[0] == 0:
    sample_products = [
        (
            master_id,
            'Wireless Noise-Cancelling Headphones',
            'Electronics',
            2499.00,
            15,
            (
                'Over-ear Bluetooth headphones with 30-hour battery life and'
                ' deep bass.'
            ),
            'placeholder.png',
        ),
        (
            master_id,
            'Smart Fitness Band',
            'Electronics',
            1499.00,
            12,
            (
                'Track heart rate, steps, sleep, and notifications with IP68'
                ' water resistance.'
            ),
            'placeholder.png',
        ),
        (
            master_id,
            'Minimalist Cotton Hoodie',
            'Fashion',
            1199.00,
            20,
            'Soft fleece-lined everyday pullover hoodie in unisex sizing.',
            'placeholder.png',
        ),
        (
            master_id,
            'Ergonomic Laptop Stand',
            'Accessories',
            899.00,
            10,
            'Adjustable aluminum stand for better posture and laptop cooling.',
            'placeholder.png',
        ),
        (
            master_id,
            'Insulated Stainless Steel Bottle',
            'Home & Kitchen',
            599.00,
            25,
            (
                'Keeps drinks cold for 24 hours or hot for 12 hours, 1L'
                ' capacity.'
            ),
            'placeholder.png',
        ),
        (
            master_id,
            'Study Desk LED Lamp',
            'Home & Kitchen',
            749.00,
            8,
            'Eye-care rechargeable desk lamp with 3 brightness modes for students.',
            'placeholder.png',
        ),
    ]
    cursor.executemany(
        """INSERT INTO products (seller_id, name, category, price, stock_qty, description, image)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
        sample_products,
    )

  cleanup_expired_closed_complaints(conn)
  conn.commit()
  conn.close()


# =========================================
# 2. Role & Access Decorators
# =========================================
def get_user_dashboard_url():
  role = session.get('role')
  if role == 'seller' and session.get('user_email') == 'sell1':
    return url_for('admin_dashboard')
  if role == 'seller':
    return url_for('seller_dashboard', seller_id=session['user_id'])
  if role == 'helpdesk':
    return url_for('help_center_dashboard')
  return url_for('customer_dashboard', user_id=session['user_id'])


def customer_required(f):
  @wraps(f)
  def decorated_function(*args, **kwargs):
    if 'user_id' not in session:
      flash('Please log in as a Customer to continue.', 'warning')
      return redirect(url_for('login', next=request.url))
    if session.get('role') != 'customer':
      flash('Customer account required.', 'danger')
      return redirect(get_user_dashboard_url())
    return f(*args, **kwargs)

  return decorated_function


def seller_required(f):
  @wraps(f)
  def decorated_function(*args, **kwargs):
    if 'user_id' not in session or session.get('role') != 'seller':
      flash('Seller access required.', 'danger')
      return redirect(url_for('login'))
    return f(*args, **kwargs)

  return decorated_function


def master_admin_required(f):
  @wraps(f)
  def decorated_function(*args, **kwargs):
    if session.get('role') != 'seller' or session.get('user_email') != 'sell1':
      flash('Master Control (sell1) access only.', 'danger')
      return redirect(
          get_user_dashboard_url() if 'user_id' in session else url_for('login')
      )
    return f(*args, **kwargs)

  return decorated_function


def helpdesk_required(f):
  @wraps(f)
  def decorated_function(*args, **kwargs):
    if 'user_id' not in session or session.get('role') != 'helpdesk':
      flash('Help Center staff credentials required.', 'danger')
      return redirect(url_for('login'))
    return f(*args, **kwargs)

  return decorated_function


# =========================================
# 3. Search, Invoice & Complaint Helpers
# =========================================
def run_ai_search(conn, query_text):
  text = query_text.lower()
  max_price = None
  min_price = None

  under_match = re.search(
      r'(?:under|below|less than|max)\s*₹?\s*(\d+(?:\.\d+)?)', text
  )
  if under_match:
    max_price = float(under_match.group(1))

  over_match = re.search(
      r'(?:above|over|more than|min)\s*₹?\s*(\d+(?:\.\d+)?)', text
  )
  if over_match:
    min_price = float(over_match.group(1))

  stop_words = {
      'for',
      'under',
      'below',
      'above',
      'over',
      'less',
      'than',
      'more',
      'the',
      'a',
      'an',
      'and',
      'or',
      'best',
      'good',
      'show',
      'me',
      'find',
      'gift',
      'gifts',
  }
  clean_words = [
      w
      for w in re.findall(r'[a-z0-9]+', text)
      if w not in stop_words and not w.isdigit()
  ]

  all_products = conn.execute("SELECT * FROM products").fetchall()
  scored = []

  for prod in all_products:
    if max_price is not None and prod['price'] > max_price:
      continue
    if min_price is not None and prod['price'] < min_price:
      continue

    haystack = (
        f"{prod['name']} {prod['category']} {prod['description'] or ''}".lower()
    )
    score = sum(1 for word in clean_words if word in haystack)

    if not clean_words or score > 0:
      scored.append((score, prod))

  scored.sort(key=lambda x: (-x[0], x[1]['price']))
  matched_products = [item[1] for item in scored]

  summary_parts = []
  if clean_words:
    summary_parts.append(f"keywords ({', '.join(clean_words)})")
  if max_price is not None:
    summary_parts.append(f"under ₹{max_price:.0f}")
  if min_price is not None:
    summary_parts.append(f"above ₹{min_price:.0f}")

  note = f"Showing {len(matched_products)} smart match(es)"
  if summary_parts:
    note += ' filtered by ' + ' & '.join(summary_parts) + '.'
  return matched_products, note


def generate_system_invoice_number(order_id, created_at=None, prefix='INV'):
  """Generates a system invoice number using order date and order ID."""
  date_part = datetime.now().strftime('%Y%m%d')
  if created_at:
    digits = ''.join(ch for ch in str(created_at) if ch.isdigit())
    if len(digits) >= 8:
      date_part = digits[:8]
  return f'{prefix}-{date_part}-{int(order_id):06d}'


def _pdf_safe_text(value):
  text = str(value or '')
  return (
      text.replace('₹', 'INR ')
      .encode('latin-1', errors='replace')
      .decode('latin-1')
  )


def build_invoice_pdf_bytes(order, items):
  pdf = FPDF(orientation='P', unit='mm', format='A4')
  pdf.set_auto_page_break(auto=True, margin=15)
  pdf.add_page()

  inv_no = generate_system_invoice_number(
      order['id'], order['created_at'], prefix='INV'
  )
  pdf.set_font('Helvetica', 'B', 18)
  pdf.cell(
      0,
      10,
      _pdf_safe_text('TAX / ORDER INVOICE'),
      new_x='LMARGIN',
      new_y='NEXT',
  )

  pdf.set_font('Helvetica', '', 10)
  pdf.cell(
      0,
      6,
      _pdf_safe_text(f"Invoice No: {inv_no}  |  Order No: #{order['id']}"),
      new_x='LMARGIN',
      new_y='NEXT',
  )
  pdf.cell(
      0,
      6,
      _pdf_safe_text(
          f"Status: {order['status']}  |  Customer: {order['customer_name']}"
      ),
      new_x='LMARGIN',
      new_y='NEXT',
  )
  pdf.multi_cell(
      0,
      6,
      _pdf_safe_text(f"Address: {order['address']}  |  Phone: {order['phone']}"),
  )
  pdf.ln(4)

  pdf.set_font('Helvetica', 'B', 10)
  pdf.cell(92, 8, 'Product Item', border=1)
  pdf.cell(22, 8, 'Qty', border=1, align='C')
  pdf.cell(36, 8, 'Unit Price', border=1, align='R')
  pdf.cell(40, 8, 'Line Total', border=1, align='R')
  pdf.ln()

  pdf.set_font('Helvetica', '', 10)
  for item in items:
    qty = int(item['quantity'])
    unit_price = float(item['price'])
    line_total = qty * unit_price
    name = _pdf_safe_text(item['product_name'])[:42]
    pdf.cell(92, 8, name, border=1)
    pdf.cell(22, 8, str(qty), border=1, align='C')
    pdf.cell(36, 8, f'INR {unit_price:,.2f}', border=1, align='R')
    pdf.cell(40, 8, f'INR {line_total:,.2f}', border=1, align='R')
    pdf.ln()

  pdf.ln(4)
  pdf.set_font('Helvetica', 'B', 12)
  pdf.cell(
      0,
      9,
      _pdf_safe_text(f"Grand Total: INR {float(order['total_price']):,.2f}"),
      align='R',
      new_x='LMARGIN',
      new_y='NEXT',
  )

  raw = pdf.output()
  return bytes(raw) if isinstance(raw, (bytearray, memoryview)) else raw


def _resolve_complaint_seller_id(conn, comp):
  if comp['seller_id']:
    return comp['seller_id']
  if comp['order_id']:
    row = conn.execute(
        """SELECT seller_id FROM order_items
           WHERE order_id = ? AND seller_id IS NOT NULL LIMIT 1""",
        (comp['order_id'],),
    ).fetchone()
    if row and row['seller_id']:
      conn.execute(
          'UPDATE complaints SET seller_id = ? WHERE id = ?',
          (row['seller_id'], comp['id']),
      )
      return row['seller_id']
  if comp['product_id']:
    prod = conn.execute(
        'SELECT seller_id FROM products WHERE id = ? AND seller_id IS NOT NULL',
        (comp['product_id'],),
    ).fetchone()
    if prod and prod['seller_id']:
      conn.execute(
          'UPDATE complaints SET seller_id = ? WHERE id = ?',
          (prod['seller_id'], comp['id']),
      )
      return prod['seller_id']
  return None


# =========================================
# 4. Storefront, Product Detail & Reviews Routes
# =========================================
@app.route('/googlec2cb7747ffbd436c.html')
def google_site_verification():
  return (
      'google-site-verification: googlec2cb7747ffbd436c.html',
      200,
      {'Content-Type': 'text/html; charset=utf-8'},
  )


@app.route('/')
def index():
  q = request.args.get('q', '').strip()
  mode = request.args.get('mode', 'standard')
  category = request.args.get('category', '').strip()

  conn = get_db()
  cleanup_expired_closed_complaints(conn)
  cat_rows = conn.execute(
      'SELECT DISTINCT category FROM products ORDER BY category'
  ).fetchall()
  categories = [row['category'] for row in cat_rows]
  ai_note = None

  if q and mode == 'ai':
    products, ai_note = run_ai_search(conn, q)
  elif q:
    like_q = f'%{q}%'
    products = conn.execute(
        """SELECT * FROM products
           WHERE name LIKE ? OR description LIKE ? OR category LIKE ?""",
        (like_q, like_q, like_q),
    ).fetchall()
  elif category:
    products = conn.execute(
        'SELECT * FROM products WHERE category = ?', (category,)
    ).fetchall()
  else:
    products = conn.execute('SELECT * FROM products ORDER BY id DESC').fetchall()

  conn.close()
  return render_template(
      'index.html', products=products, categories=categories, ai_note=ai_note
  )


@app.route('/product/<int:product_id>')
def product_detail(product_id):
  conn = get_db()
  product = conn.execute(
      """SELECT p.*, u.name AS seller_name
           FROM products p
           LEFT JOIN users u ON p.seller_id = u.id
           WHERE p.id = ?""",
      (product_id,),
  ).fetchone()
  if not product:
    conn.close()
    flash('Product not found.', 'danger')
    return redirect(url_for('index'))

  reviews = conn.execute(
      """SELECT * FROM product_reviews
           WHERE product_id = ? ORDER BY created_at DESC, id DESC""",
      (product_id,),
  ).fetchall()
  avg_row = conn.execute(
      """SELECT AVG(rating) AS avg_rating, COUNT(*) AS review_count
           FROM product_reviews WHERE product_id = ?""",
      (product_id,),
  ).fetchone()
  conn.close()

  avg_rating = (
      round(float(avg_row['avg_rating']), 1)
      if avg_row and avg_row['avg_rating'] is not None
      else 0.0
  )
  review_count = int(avg_row['review_count']) if avg_row else 0

  return render_template(
      'product_detail.html',
      product=product,
      reviews=reviews,
      avg_rating=avg_rating,
      review_count=review_count,
  )


@app.route('/product/<int:product_id>/review', methods=['POST'])
def add_product_review(product_id):
  rating_raw = request.form.get('rating', '').strip()
  comment = request.form.get('comment', '').strip()
  reviewer_name = (
      request.form.get('reviewer_name', '').strip()
      or session.get('user_name')
      or 'Guest Customer'
  )

  if not rating_raw.isdigit() or not (1 <= int(rating_raw) <= 5):
    flash('Please select a rating from 1 to 5 stars.', 'warning')
    return redirect(url_for('product_detail', product_id=product_id))

  if not comment:
    flash('Please write a comment about the product.', 'warning')
    return redirect(url_for('product_detail', product_id=product_id))

  rating = int(rating_raw)
  conn = get_db()
  product = conn.execute(
      'SELECT id FROM products WHERE id = ?', (product_id,)
  ).fetchone()
  if not product:
    conn.close()
    flash('Product not found.', 'danger')
    return redirect(url_for('index'))

  conn.execute(
      """INSERT INTO product_reviews (product_id, user_id, reviewer_name, rating, comment)
           VALUES (?, ?, ?, ?, ?)""",
      (product_id, session.get('user_id'), reviewer_name, rating, comment),
  )
  conn.commit()
  conn.close()

  flash('Thank you! Your rating and comment have been posted.', 'success')
  return redirect(url_for('product_detail', product_id=product_id))


# =========================================
# 5. Shopping Cart Routes
# =========================================
@app.route('/cart')
def view_cart():
  cart = session.get('cart', {})
  return render_template('cart.html', cart=cart)


@app.route('/cart/add/<int:product_id>')
def add_to_cart(product_id):
  conn = get_db()
  product = conn.execute(
      'SELECT * FROM products WHERE id = ?', (product_id,)
  ).fetchone()
  conn.close()

  if not product:
    flash('Product not found.', 'danger')
    return redirect(url_for('index'))

  available_stock = (
      product['stock_qty'] if product['stock_qty'] is not None else 0
  )
  if available_stock <= 0:
    flash(f"Sorry, '{product['name']}' is currently out of stock.", 'danger')
    return redirect(request.referrer or url_for('index'))

  cart = session.get('cart', {})
  pid = str(product_id)
  current_qty = cart[pid]['quantity'] if pid in cart else 0

  if current_qty + 1 > available_stock:
    flash(
        f"Only {available_stock} unit(s) of '{product['name']}' available in"
        ' stock.',
        'warning',
    )
    return redirect(request.referrer or url_for('index'))

  if pid in cart:
    cart[pid]['quantity'] += 1
    cart[pid]['stock_qty'] = available_stock
  else:
    cart[pid] = {
        'name': product['name'],
        'price': float(product['price']),
        'quantity': 1,
        'stock_qty': available_stock,
        'seller_id': product['seller_id'],
    }

  session['cart'] = cart
  flash(f"Added '{product['name']}' to your cart.", 'success')
  return redirect(request.referrer or url_for('index'))


@app.route('/cart/increase/<product_id>')
def increase_qty(product_id):
  cart = session.get('cart', {})
  if product_id in cart:
    conn = get_db()
    prod = conn.execute(
        'SELECT stock_qty FROM products WHERE id = ?', (int(product_id),)
    ).fetchone()
    conn.close()
    max_stock = (
        prod['stock_qty'] if (prod and prod['stock_qty'] is not None) else 0
    )
    if cart[product_id]['quantity'] + 1 > max_stock:
      flash(f'Maximum available stock ({max_stock}) reached.', 'warning')
    else:
      cart[product_id]['quantity'] += 1
      session['cart'] = cart
  return redirect(url_for('view_cart'))


@app.route('/cart/decrease/<product_id>')
def decrease_qty(product_id):
  cart = session.get('cart', {})
  if product_id in cart:
    if cart[product_id]['quantity'] > 1:
      cart[product_id]['quantity'] -= 1
    else:
      del cart[product_id]
    session['cart'] = cart
  return redirect(url_for('view_cart'))


@app.route('/cart/remove/<product_id>')
def remove_from_cart(product_id):
  cart = session.get('cart', {})
  if product_id in cart:
    del cart[product_id]
    session['cart'] = cart
    flash('Item removed from cart.', 'info')
  return redirect(url_for('view_cart'))


# =========================================
# 6. Authentication Routes
# =========================================
@app.route('/register', methods=['GET', 'POST'])
def register():
  if request.method == 'POST':
    role = request.form.get('role', 'customer')
    if role not in ('customer', 'seller'):
      role = 'customer'

    name = request.form['name'].strip()
    identifier = request.form['email'].strip()
    if role == 'customer':
      identifier = identifier.lower()

    password = request.form['password']
    confirm_password = request.form.get('confirm_password', password)

    if password != confirm_password:
      flash('Passwords do not match.', 'danger')
      return render_template('register.html')

    conn = get_db()
    try:
      cursor = conn.cursor()
      cursor.execute(
          'INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)',
          (name, identifier, generate_password_hash(password), role),
      )
      new_user_id = cursor.lastrowid
      conn.commit()
      flash(
          f'{role.capitalize()} account #{new_user_id} created! Please log in.',
          'success',
      )
      return redirect(url_for('login'))
    except sqlite3.IntegrityError:
      label = 'Username' if role == 'seller' else 'Email'
      flash(f'That {label} is already registered.', 'danger')
    finally:
      conn.close()

  return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
  if request.method == 'POST':
    login_type = request.form.get('login_type', 'customer')
    identifier = request.form['email'].strip()
    if login_type == 'customer':
      identifier = identifier.lower()
    password = request.form['password']

    conn = get_db()
    cleanup_expired_closed_complaints(conn)
    user = conn.execute(
        'SELECT * FROM users WHERE email = ? AND role = ?',
        (identifier, login_type),
    ).fetchone()
    conn.close()

    if user and check_password_hash(user['password'], password):
      session['user_id'] = user['id']
      session['user_name'] = user['name']
      session['user_email'] = user['email']
      session['role'] = user['role']

      flash(f"Welcome back, {user['name']}!", 'success')

      if user['role'] == 'seller' and user['email'] == 'sell1':
        return redirect(url_for('admin_dashboard'))
      if user['role'] == 'seller':
        return redirect(url_for('seller_dashboard', seller_id=user['id']))
      if user['role'] == 'helpdesk':
        return redirect(url_for('help_center_dashboard'))

      next_page = request.args.get('next')
      return redirect(
          next_page or url_for('customer_dashboard', user_id=user['id'])
      )
    else:
      flash(f'Invalid {login_type} credentials.', 'danger')

  return render_template('login.html')


@app.route('/logout')
def logout():
  session.clear()
  flash('You have been logged out.', 'info')
  return redirect(url_for('index'))


# =========================================
# 7. Customer Dashboard, Invoices, Order Lookup, Profile & Complaints
# =========================================
@app.route('/customer/<int:user_id>')
@customer_required
def customer_dashboard(user_id):
  if session['user_id'] != user_id:
    flash(
        'Access denied: redirected to your own customer dashboard.', 'warning'
    )
    return redirect(url_for('customer_dashboard', user_id=session['user_id']))

  conn = get_db()
  cleanup_expired_closed_complaints(conn)
  customer = conn.execute(
      'SELECT * FROM users WHERE id = ?', (user_id,)
  ).fetchone()
  orders = conn.execute(
      'SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC',
      (user_id,),
  ).fetchall()

  order_list = []
  for order in orders:
    items = conn.execute(
        'SELECT * FROM order_items WHERE order_id = ?', (order['id'],)
    ).fetchall()
    order_list.append({'info': order, 'items': items})

  products = conn.execute(
      'SELECT id, name, seller_id FROM products ORDER BY name'
  ).fetchall()
  complaints = conn.execute(
      """SELECT c.*, p.name AS product_name
           FROM complaints c
           LEFT JOIN products p ON c.product_id = p.id
           WHERE c.customer_id = ?
           ORDER BY c.created_at DESC""",
      (user_id,),
  ).fetchall()
  conn.close()

  return render_template(
      'customer_dashboard.html',
      customer=customer,
      orders=order_list,
      products=products or [],
      complaints=complaints or [],
      complaint_types=COMPLAINT_TYPES,
      invoice_statuses=INVOICE_ELIGIBLE_STATUSES,
  )


@app.route('/customer/<int:user_id>/update-profile', methods=['POST'])
@customer_required
def update_customer_profile(user_id):
  if session['user_id'] != user_id:
    flash('Access denied.', 'danger')
    return redirect(url_for('customer_dashboard', user_id=session['user_id']))

  name = request.form.get('name', '').strip()
  email = request.form.get('email', '').strip().lower()
  new_password = request.form.get('new_password', '').strip()
  confirm_password = request.form.get('confirm_password', '').strip()

  if not name or not email:
    flash('Name and user ID / email are required.', 'warning')
    return redirect(url_for('customer_dashboard', user_id=user_id))

  if new_password and new_password != confirm_password:
    flash('New password and confirm password do not match.', 'danger')
    return redirect(url_for('customer_dashboard', user_id=user_id))

  conn = get_db()
  try:
    if new_password:
      conn.execute(
          """UPDATE users SET name = ?, email = ?, password = ?
                   WHERE id = ? AND role = 'customer'""",
          (name, email, generate_password_hash(new_password), user_id),
      )
    else:
      conn.execute(
          """UPDATE users SET name = ?, email = ?
                   WHERE id = ? AND role = 'customer'""",
          (name, email, user_id),
      )
    conn.commit()
    session['user_name'] = name
    session['user_email'] = email
    flash('Your user ID and password have been updated.', 'success')
  except sqlite3.IntegrityError:
    flash(
        'That user ID / email is already registered to another account.',
        'danger',
    )
  finally:
    conn.close()

  return redirect(url_for('customer_dashboard', user_id=user_id))


@app.route('/api/order-lookup/<int:order_id>')
@customer_required
def api_order_lookup(order_id):
  conn = get_db()
  order = conn.execute(
      'SELECT * FROM orders WHERE id = ? AND user_id = ?',
      (order_id, session['user_id']),
  ).fetchone()
  if not order:
    conn.close()
    return (
        jsonify({
            'found': False,
            'message': f'Order #{order_id} not found on your account.',
        }),
        404,
    )

  items = conn.execute(
      'SELECT * FROM order_items WHERE order_id = ? ORDER BY id', (order_id,)
  ).fetchall()
  conn.close()

  item_summaries = [
      f"{row['product_name']} (x{row['quantity']})" for row in items
  ]
  first_product_id = (
      items[0]['product_id'] if items and items[0]['product_id'] else ''
  )
  first_product_name = items[0]['product_name'] if items else ''

  return jsonify({
      'found': True,
      'order_id': order['id'],
      'status': order['status'],
      'total_price': float(order['total_price']),
      'payment_method': order['payment_method'] or 'Cash on Delivery',
      'transaction_id': order['transaction_id'] or '',
      'first_product_id': first_product_id,
      'items_text': ', '.join(item_summaries),
      'suggested_subject': (
          f"Issue with Order #{order['id']} - {first_product_name}".strip(' -')
      ),
      'suggested_message': (
          f"Order #{order['id']} (Status: {order['status']}, Total:"
          f" ₹{float(order['total_price']):.2f}, Items:"
          f" {', '.join(item_summaries)}). Details: "
      ),
  })


@app.route('/order/<int:order_id>/invoice')
@customer_required
def view_buyer_invoice(order_id):
  conn = get_db()
  order = conn.execute(
      'SELECT * FROM orders WHERE id = ? AND user_id = ?',
      (order_id, session['user_id']),
  ).fetchone()

  if not order:
    conn.close()
    flash('Order not found.', 'danger')
    return redirect(url_for('customer_dashboard', user_id=session['user_id']))

  if order['status'] not in INVOICE_ELIGIBLE_STATUSES:
    conn.close()
    flash(
        f'Invoice is available once Order #{order_id} is Shipped.', 'warning'
    )
    return redirect(url_for('customer_dashboard', user_id=session['user_id']))

  items = conn.execute(
      'SELECT * FROM order_items WHERE order_id = ? ORDER BY id', (order_id,)
  ).fetchall()
  conn.close()

  return render_template(
      'invoice.html',
      doc_title='Order Invoice',
      doc_badge='BUYER INVOICE',
      invoice_number=generate_system_invoice_number(
          order['id'], order['created_at'], prefix='INV'
      ),
      generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
      order=order,
      items=items,
      complaint=None,
      is_refund_doc=False,
      back_url=url_for('customer_dashboard', user_id=session['user_id']),
  )


@app.route('/order/<int:order_id>/invoice.pdf')
@customer_required
def download_invoice_pdf(order_id):
  conn = get_db()
  order = conn.execute(
      'SELECT * FROM orders WHERE id = ? AND user_id = ?',
      (order_id, session['user_id']),
  ).fetchone()

  if not order:
    conn.close()
    flash('Order not found.', 'danger')
    return redirect(url_for('customer_dashboard', user_id=session['user_id']))

  if order['status'] not in INVOICE_ELIGIBLE_STATUSES:
    conn.close()
    flash(
        f'Invoice PDF is available once Order #{order_id} is Shipped.',
        'warning',
    )
    return redirect(url_for('customer_dashboard', user_id=session['user_id']))

  items = conn.execute(
      'SELECT * FROM order_items WHERE order_id = ? ORDER BY id', (order_id,)
  ).fetchall()
  conn.close()

  pdf_bytes = build_invoice_pdf_bytes(order, items)
  return send_file(
      BytesIO(pdf_bytes),
      mimetype='application/pdf',
      as_attachment=True,
      download_name=f"invoice_order_{order['id']}.pdf",
  )


@app.route('/complaint/submit', methods=['POST'])
@customer_required
def submit_complaint():
  order_raw = request.form.get('order_id', '').replace('#', '').strip()
  order_id = int(order_raw) if order_raw.isdigit() else None

  product_raw = request.form.get('product_id', '').strip()
  product_id = int(product_raw) if product_raw.isdigit() else None

  complaint_type = request.form.get(
      'complaint_type', 'Product Issue Complaint'
  ).strip()
  if complaint_type not in COMPLAINT_TYPES:
    complaint_type = 'Product Issue Complaint'

  subject = request.form['subject'].strip()
  message = request.form['message'].strip()

  conn = get_db()
  cleanup_expired_closed_complaints(conn)
  seller_id = None
  if product_id:
    prod = conn.execute(
        'SELECT seller_id FROM products WHERE id = ?', (product_id,)
    ).fetchone()
    if prod:
      seller_id = prod['seller_id']
  elif order_id:
    item = conn.execute(
        'SELECT seller_id, product_id FROM order_items WHERE order_id = ? LIMIT 1',
        (order_id,),
    ).fetchone()
    if item:
      seller_id = item['seller_id']
      product_id = item['product_id']

  conn.execute(
      """INSERT INTO complaints
           (customer_id, customer_name, order_id, product_id, seller_id, complaint_type, subject, message,
            help_center_access, escalated_to_admin, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 'In Help Desk')""",
      (
          session['user_id'],
          session['user_name'],
          order_id,
          product_id,
          seller_id,
          complaint_type,
          subject,
          message,
      ),
  )
  conn.commit()
  conn.close()

  flash(
      f'Your {complaint_type} has been sent to the Help Desk for review.',
      'success',
  )
  return redirect(url_for('customer_dashboard', user_id=session['user_id']))


@app.route('/checkout', methods=['GET', 'POST'])
@customer_required
def checkout():
  cart = session.get('cart', {})
  if not cart:
    flash('Your cart is empty.', 'warning')
    return redirect(url_for('index'))

  total_price = sum(item['price'] * item['quantity'] for item in cart.values())
  seller_ids = {
      item.get('seller_id') for item in cart.values() if item.get('seller_id')
  }

  conn = get_db()
  seller_qrs = []
  for sid in seller_ids:
    s_row = conn.execute(
        "SELECT id, name, qr_code FROM users WHERE id = ? AND role = 'seller'",
        (sid,),
    ).fetchone()
    if s_row:
      seller_qrs.append(s_row)

  if request.method == 'POST':
    customer_name = request.form['customer_name'].strip()
    address = request.form['address'].strip()
    phone = request.form['phone'].strip()
    payment_method = request.form.get('payment_method', 'Cash on Delivery')
    transaction_id = request.form.get('transaction_id', '').strip()

    if payment_method == 'Online QR Payment' and not transaction_id:
      conn.close()
      flash(
          'Please scan the QR code and enter your UPI / Transaction Reference'
          ' ID.',
          'danger',
      )
      return render_template(
          'checkout.html',
          cart=cart,
          total_price=total_price,
          seller_qrs=seller_qrs,
      )

    for pid, item in cart.items():
      if pid.isdigit():
        prod = conn.execute(
            'SELECT stock_qty FROM products WHERE id = ?', (int(pid),)
        ).fetchone()
        if prod is None or (prod['stock_qty'] or 0) < item['quantity']:
          conn.close()
          flash(f"Insufficient stock for '{item['name']}'.", 'danger')
          return redirect(url_for('view_cart'))

    payment_status = (
        'Pending Verification'
        if payment_method == 'Online QR Payment'
        else 'COD'
    )
    txn_value = (
        transaction_id if payment_method == 'Online QR Payment' else 'COD'
    )

    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO orders (user_id, customer_name, address, phone, total_price, payment_method, transaction_id, payment_status, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Order Placed')""",
        (
            session['user_id'],
            customer_name,
            address,
            phone,
            total_price,
            payment_method,
            txn_value,
            payment_status,
        ),
    )
    order_id = cursor.lastrowid

    for pid, item in cart.items():
      prod_id = int(pid) if pid.isdigit() else None
      cursor.execute(
          """INSERT INTO order_items (order_id, product_id, seller_id, product_name, quantity, price)
                   VALUES (?, ?, ?, ?, ?, ?)""",
          (
              order_id,
              prod_id,
              item.get('seller_id'),
              item['name'],
              item['quantity'],
              item['price'],
          ),
      )
      if prod_id is not None:
        cursor.execute(
            """UPDATE products SET stock_qty = MAX(0, stock_qty - ?)
                       WHERE id = ?""",
            (item['quantity'], prod_id),
        )

    conn.commit()
    conn.close()

    session.pop('cart', None)
    flash(f'Order placed! Your Order Number is #{order_id}.', 'success')
    return redirect(url_for('track_order', order_id=order_id))

  conn.close()
  return render_template(
      'checkout.html',
      cart=cart,
      total_price=total_price,
      seller_qrs=seller_qrs,
  )


@app.route('/order/cancel/<int:order_id>', methods=['POST'])
@customer_required
def cancel_order(order_id):
  conn = get_db()
  order = conn.execute(
      'SELECT * FROM orders WHERE id = ? AND user_id = ?',
      (order_id, session['user_id']),
  ).fetchone()

  if not order:
    conn.close()
    flash('Order not found.', 'danger')
    return redirect(url_for('customer_dashboard', user_id=session['user_id']))

  if order['status'] not in ('Order Placed', 'Confirmed'):
    conn.close()
    flash(
        f"Order #{order_id} cannot be cancelled once '{order['status']}'.",
        'warning',
    )
    return redirect(url_for('customer_dashboard', user_id=session['user_id']))

  items = conn.execute(
      'SELECT product_id, quantity FROM order_items WHERE order_id = ?',
      (order_id,),
  ).fetchall()
  for item in items:
    if item['product_id'] is not None:
      conn.execute(
          'UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?',
          (item['quantity'], item['product_id']),
      )

  conn.execute(
      "UPDATE orders SET status = 'Cancelled' WHERE id = ?", (order_id,)
  )
  conn.commit()
  conn.close()

  flash(f'Order #{order_id} cancelled and stock restored.', 'info')
  return redirect(url_for('customer_dashboard', user_id=session['user_id']))


@app.route('/my-orders')
@customer_required
def my_orders():
  return redirect(url_for('customer_dashboard', user_id=session['user_id']))


@app.route('/track', methods=['GET', 'POST'])
def track_order():
  order_id = request.args.get('order_id') or request.form.get('order_id')
  order = None
  items = []

  if order_id:
    clean_id = str(order_id).replace('#', '').strip()
    if clean_id.isdigit():
      conn = get_db()
      order = conn.execute(
          'SELECT * FROM orders WHERE id = ?', (int(clean_id),)
      ).fetchone()
      if order:
        items = conn.execute(
            'SELECT * FROM order_items WHERE order_id = ?', (order['id'],)
        ).fetchall()
      else:
        flash(f'No order found with Order Number #{clean_id}.', 'danger')
      conn.close()
    else:
      flash('Please enter a valid numeric Order Number.', 'warning')

  return render_template(
      'tracking.html', order=order, items=items, statuses=ORDER_STATUSES
  )


# =========================================
# 8. Registered Seller Dashboard (Own Panel, Profile, Invoice Copy & Refund Proof)
# =========================================
@app.route('/seller/<int:seller_id>')
@seller_required
def seller_dashboard(seller_id):
  if session.get('user_email') == 'sell1':
    flash(
        'Master Admin cannot visit individual seller panels. Use /admin.',
        'warning',
    )
    return redirect(url_for('admin_dashboard'))

  if session['user_id'] != seller_id:
    flash(
        'Access denied: redirected to your own seller dashboard.', 'warning'
    )
    return redirect(url_for('seller_dashboard', seller_id=session['user_id']))

  conn = get_db()
  cleanup_expired_closed_complaints(conn)
  seller = conn.execute(
      "SELECT * FROM users WHERE id = ? AND role = 'seller'", (seller_id,)
  ).fetchone()
  if not seller:
    conn.close()
    flash('Seller account not found.', 'danger')
    return redirect(url_for('index'))

  my_products = conn.execute(
      'SELECT * FROM products WHERE seller_id = ? ORDER BY id DESC',
      (seller_id,),
  ).fetchall()

  order_rows = conn.execute(
      """
        SELECT DISTINCT o.*
        FROM orders o
        JOIN order_items oi ON o.id = oi.order_id
        WHERE oi.seller_id = ?
        ORDER BY o.created_at DESC
    """,
      (seller_id,),
  ).fetchall()

  seller_orders = []
  for order in order_rows:
    items = conn.execute(
        'SELECT * FROM order_items WHERE order_id = ? AND seller_id = ?',
        (order['id'], seller_id),
    ).fetchall()
    seller_orders.append({'info': order, 'items': items})

  notices = conn.execute(
      'SELECT * FROM seller_notices WHERE seller_id = ? ORDER BY created_at DESC',
      (seller_id,),
  ).fetchall()

  complaint_notices = conn.execute(
      """SELECT c.*, p.name AS product_name
           FROM complaints c
           LEFT JOIN products p ON c.product_id = p.id
           WHERE (
                 c.seller_id = ?
                 OR (
                     c.order_id IS NOT NULL
                     AND c.order_id IN (SELECT order_id FROM order_items WHERE seller_id = ?)
                 )
             )
             AND (
                 c.refund_approved = 1
                 OR c.status IN ('Awaiting Seller Refund Proof', 'Refund Proof Submitted')
                 OR (c.seller_notice IS NOT NULL AND c.seller_notice != '')
             )
           ORDER BY c.created_at DESC""",
      (seller_id, seller_id),
  ).fetchall()

  conn.close()
  return render_template(
      'seller.html',
      seller=seller,
      products=my_products,
      orders=seller_orders,
      notices=notices,
      complaint_notices=complaint_notices,
      statuses=ORDER_STATUSES,
      payment_statuses=PAYMENT_STATUSES,
      invoice_statuses=INVOICE_ELIGIBLE_STATUSES,
  )


@app.route('/seller/<int:seller_id>/update-profile', methods=['POST'])
@seller_required
def update_seller_profile(seller_id):
  if session.get('user_email') == 'sell1':
    flash('Use /admin to manage Master Admin settings.', 'warning')
    return redirect(url_for('admin_dashboard'))

  if session['user_id'] != seller_id:
    flash('Access denied.', 'danger')
    return redirect(url_for('seller_dashboard', seller_id=session['user_id']))

  name = request.form.get('name', '').strip()
  user_id_login = request.form.get('user_id_login', '').strip()
  new_password = request.form.get('new_password', '').strip()
  confirm_password = request.form.get('confirm_password', '').strip()

  if not name or not user_id_login:
    flash('Seller name and user ID are required.', 'warning')
    return redirect(url_for('seller_dashboard', seller_id=seller_id))

  if new_password and new_password != confirm_password:
    flash('New password and confirm password do not match.', 'danger')
    return redirect(url_for('seller_dashboard', seller_id=seller_id))

  conn = get_db()
  try:
    if new_password:
      conn.execute(
          """UPDATE users SET name = ?, email = ?, password = ?
                   WHERE id = ? AND role = 'seller'""",
          (name, user_id_login, generate_password_hash(new_password), seller_id),
      )
    else:
      conn.execute(
          """UPDATE users SET name = ?, email = ?
                   WHERE id = ? AND role = 'seller'""",
          (name, user_id_login, seller_id),
      )
    conn.commit()
    session['user_name'] = name
    session['user_email'] = user_id_login
    flash('Your seller user ID, name, and password have been updated.', 'success')
  except sqlite3.IntegrityError:
    flash(
        f"User ID '{user_id_login}' is already taken by another account.",
        'danger',
    )
  finally:
    conn.close()

  return redirect(url_for('seller_dashboard', seller_id=seller_id))


@app.route('/seller/order/<int:order_id>/invoice')
@seller_required
def view_seller_invoice(order_id):
  conn = get_db()
  order = conn.execute(
      'SELECT * FROM orders WHERE id = ?', (order_id,)
  ).fetchone()
  if not order:
    conn.close()
    flash('Order not found.', 'danger')
    return redirect(get_user_dashboard_url())

  if session.get('user_email') == 'sell1':
    items = conn.execute(
        'SELECT * FROM order_items WHERE order_id = ? ORDER BY id', (order_id,)
    ).fetchall()
    back_url = url_for('admin_dashboard')
  else:
    items = conn.execute(
        """SELECT * FROM order_items
           WHERE order_id = ? AND seller_id = ? ORDER BY id""",
        (order_id, session['user_id']),
    ).fetchall()
    if not items:
      conn.close()
      flash(
          'Access denied: this order does not contain your products.', 'danger'
      )
      return redirect(url_for('seller_dashboard', seller_id=session['user_id']))
    back_url = url_for('seller_dashboard', seller_id=session['user_id'])

  conn.close()

  if order['status'] not in INVOICE_ELIGIBLE_STATUSES:
    flash(
        f'Invoice is available once Order #{order_id} is Shipped.', 'warning'
    )
    return redirect(back_url)

  return render_template(
      'invoice.html',
      doc_title='Seller Order Invoice Copy',
      doc_badge='SELLER INVOICE COPY',
      invoice_number=generate_system_invoice_number(
          order['id'], order['created_at'], prefix='INV'
      ),
      generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
      order=order,
      items=items,
      complaint=None,
      is_refund_doc=False,
      back_url=back_url,
  )


@app.route('/seller/complaint/<int:complaint_id>/refund-proof', methods=['POST'])
@seller_required
def submit_seller_refund_proof(complaint_id):
  refund_txn_id = request.form.get('seller_refund_txn_id', '').strip()
  if not refund_txn_id:
    flash(
        'Please enter a valid Refund UPI ID or Transaction Reference ID.',
        'danger',
    )
    return redirect(get_user_dashboard_url())

  conn = get_db()
  comp = conn.execute(
      """SELECT * FROM complaints
           WHERE id = ?
             AND (
                 seller_id = ?
                 OR (
                     order_id IS NOT NULL
                     AND order_id IN (SELECT order_id FROM order_items WHERE seller_id = ?)
                 )
             )""",
      (complaint_id, session['user_id'], session['user_id']),
  ).fetchone()

  if not comp:
    conn.close()
    flash('Complaint record not found for your seller account.', 'danger')
    return redirect(get_user_dashboard_url())

  conn.execute(
      """UPDATE complaints
           SET seller_id = COALESCE(seller_id, ?),
               seller_refund_txn_id = ?,
               refund_approved = 1,
               refund_verified = 0,
               closed_at = NULL,
               status = 'Refund Proof Submitted'
           WHERE id = ?""",
      (session['user_id'], refund_txn_id, complaint_id),
  )
  conn.commit()
  conn.close()

  flash(
      f'Refund Transaction / UPI ID ({refund_txn_id}) sent to Admin for'
      ' verification.',
      'success',
  )
  return redirect(get_user_dashboard_url())


@app.route('/seller/upload-qr', methods=['POST'])
@seller_required
def upload_seller_qr():
  qr_file = request.files.get('qr_code')
  if not qr_file or not qr_file.filename:
    flash('Please select a QR scanner image to upload.', 'warning')
    return redirect(get_user_dashboard_url())

  safe_name = f"qr_seller_{session['user_id']}_{secure_filename(qr_file.filename)}"
  qr_file.save(os.path.join(UPLOAD_FOLDER, safe_name))

  conn = get_db()
  conn.execute(
      'UPDATE users SET qr_code = ? WHERE id = ?',
      (safe_name, session['user_id']),
  )
  conn.commit()
  conn.close()

  flash('Your Payment QR Code scanner has been updated.', 'success')
  return redirect(get_user_dashboard_url())


# =========================================
# 9. Help Center Dashboard (/helpdesk — First Line Support)
# =========================================
@app.route('/helpdesk')
@helpdesk_required
def help_center_dashboard():
  conn = get_db()
  cleanup_expired_closed_complaints(conn)
  complaints = conn.execute("""
        SELECT c.*, p.name AS product_name, u.name AS seller_name, u.email AS seller_email
        FROM complaints c
        LEFT JOIN products p ON c.product_id = p.id
        LEFT JOIN users u ON c.seller_id = u.id
        ORDER BY c.created_at DESC
    """).fetchall()
  conn.close()

  return render_template(
      'help_center.html',
      complaints=complaints,
      complaint_statuses=COMPLAINT_STATUSES,
  )


@app.route('/helpdesk/complaint/<int:complaint_id>/respond', methods=['POST'])
@helpdesk_required
def help_center_respond(complaint_id):
  action = request.form.get('action', 'solve')
  help_reply = request.form.get('help_reply', '').strip()
  seller_notice = request.form.get('seller_notice', '').strip()
  escalation_note = request.form.get('escalation_note', '').strip()

  conn = get_db()
  comp = conn.execute(
      'SELECT * FROM complaints WHERE id = ?', (complaint_id,)
  ).fetchone()

  if comp:
    target_seller_id = _resolve_complaint_seller_id(conn, comp)
    if action == 'escalate':
      note_text = (
          escalation_note
          or help_reply
          or (
              f"Escalated by Help Desk ({session.get('user_email')}) for Admin"
              ' refund/final resolution.'
          )
      )
      conn.execute(
          """UPDATE complaints
                   SET help_reply = COALESCE(NULLIF(?, ''), help_reply),
                       escalated_to_admin = 1,
                       escalation_note = ?,
                       closed_at = NULL,
                       status = 'Escalated to Admin'
                   WHERE id = ?""",
          (help_reply, note_text, complaint_id),
      )
      conn.commit()
      flash(
          f'Complaint #{complaint_id} escalated to Admin for refund / final'
          ' solution.',
          'warning',
      )
    elif action == 'close':
      conn.execute(
          """UPDATE complaints
                   SET help_reply = COALESCE(NULLIF(?, ''), help_reply),
                       status = 'Closed',
                       closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP)
                   WHERE id = ?""",
          (help_reply, complaint_id),
      )
      conn.commit()
      flash(
          f'Complaint #{complaint_id} closed. It will be automatically deleted'
          f' after {AUTO_DELETE_CLOSED_DAYS} days.',
          'success',
      )
    else:
      final_status = 'Notice Sent to Seller' if seller_notice else 'Resolved'
      if seller_notice and target_seller_id:
        conn.execute(
            """INSERT INTO seller_notices
                       (seller_id, seller_identifier, sent_by, message)
                       VALUES (?, ?, ?, ?)""",
            (
                target_seller_id,
                f'Seller #{target_seller_id}',
                f"Help Center ({session.get('user_email')})",
                seller_notice,
            ),
        )

      conn.execute(
          """UPDATE complaints
                   SET help_reply = ?,
                       seller_notice = COALESCE(NULLIF(?, ''), seller_notice),
                       closed_at = NULL,
                       status = ?
                   WHERE id = ?""",
          (help_reply, seller_notice, final_status, complaint_id),
      )
      conn.commit()
      flash(f'Solution sent for Complaint #{complaint_id}.', 'success')
  conn.close()

  return redirect(url_for('help_center_dashboard'))


# =========================================
# 10. Master Admin Panel (/admin — Escalated Complaints, Refunds & Invoices)
# =========================================
@app.route('/admin')
@master_admin_required
def admin_dashboard():
  conn = get_db()
  cleanup_expired_closed_complaints(conn)
  admin_user = conn.execute(
      'SELECT * FROM users WHERE id = ?', (session['user_id'],)
  ).fetchone()

  sellers = conn.execute(
      "SELECT * FROM users WHERE role = 'seller' AND email != 'sell1' ORDER BY id"
  ).fetchall()
  customers = conn.execute(
      "SELECT * FROM users WHERE role = 'customer' ORDER BY id"
  ).fetchall()
  help_staff = conn.execute(
      "SELECT * FROM users WHERE role = 'helpdesk' ORDER BY id"
  ).fetchall()

  products = conn.execute("""
        SELECT p.*, u.name AS seller_name
        FROM products p
        LEFT JOIN users u ON p.seller_id = u.id
        ORDER BY p.id DESC
    """).fetchall()

  orders = conn.execute('SELECT * FROM orders ORDER BY created_at DESC').fetchall()
  all_orders = []
  for order in orders:
    items = conn.execute(
        'SELECT * FROM order_items WHERE order_id = ?', (order['id'],)
    ).fetchall()
    all_orders.append({'info': order, 'items': items})

  complaints = conn.execute("""
        SELECT c.*, p.name AS product_name, u.name AS seller_name
        FROM complaints c
        LEFT JOIN products p ON c.product_id = p.id
        LEFT JOIN users u ON c.seller_id = u.id
        ORDER BY c.escalated_to_admin DESC, c.created_at DESC
    """).fetchall()

  conn.close()

  return render_template(
      'admin.html',
      admin_user=admin_user,
      sellers=sellers,
      seller_count=len(sellers),
      customers=customers,
      help_staff=help_staff,
      products=products,
      orders=all_orders,
      complaints=complaints,
      statuses=ORDER_STATUSES,
      payment_statuses=PAYMENT_STATUSES,
      complaint_statuses=COMPLAINT_STATUSES,
      invoice_statuses=INVOICE_ELIGIBLE_STATUSES,
  )


@app.route('/admin/refund-invoice/<int:complaint_id>')
@master_admin_required
def view_admin_refund_invoice(complaint_id):
  conn = get_db()
  complaint = conn.execute(
      'SELECT * FROM complaints WHERE id = ?', (complaint_id,)
  ).fetchone()
  if not complaint or not complaint['order_id']:
    conn.close()
    flash('Refund complaint or linked order not found.', 'danger')
    return redirect(url_for('admin_dashboard'))

  order = conn.execute(
      'SELECT * FROM orders WHERE id = ?', (complaint['order_id'],)
  ).fetchone()
  if not order:
    conn.close()
    flash('Linked order not found.', 'danger')
    return redirect(url_for('admin_dashboard'))

  items = conn.execute(
      'SELECT * FROM order_items WHERE order_id = ? ORDER BY id', (order['id'],)
  ).fetchall()
  conn.close()

  return render_template(
      'invoice.html',
      doc_title='Admin Refund Invoice',
      doc_badge='ADMIN REFUND / CREDIT INVOICE',
      invoice_number=generate_system_invoice_number(
          order['id'], order['created_at'], prefix='REF'
      ),
      generated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
      order=order,
      items=items,
      complaint=complaint,
      is_refund_doc=True,
      back_url=url_for('admin_dashboard'),
  )


@app.route('/admin/edit-seller/<int:seller_id>', methods=['POST'])
@master_admin_required
def edit_seller_account(seller_id):
  name = request.form.get('seller_name', '').strip()
  username = request.form.get('seller_username', '').strip()
  password = request.form.get('seller_password', '').strip()

  if not name or not username:
    flash('Seller name and user ID are required.', 'warning')
    return redirect(url_for('admin_dashboard'))

  conn = get_db()
  seller = conn.execute(
      """SELECT * FROM users
           WHERE id = ? AND role = 'seller' AND email != 'sell1'""",
      (seller_id,),
  ).fetchone()
  if not seller:
    conn.close()
    flash('Seller account not found.', 'danger')
    return redirect(url_for('admin_dashboard'))

  try:
    if password:
      conn.execute(
          """UPDATE users SET name = ?, email = ?, password = ?
                   WHERE id = ? AND role = 'seller'""",
          (name, username, generate_password_hash(password), seller_id),
      )
    else:
      conn.execute(
          """UPDATE users SET name = ?, email = ?
                   WHERE id = ? AND role = 'seller'""",
          (name, username, seller_id),
      )
    conn.commit()
    flash(f"Seller account '{username}' updated.", 'success')
  except sqlite3.IntegrityError:
    flash(f"User ID '{username}' is already taken by another account.", 'danger')
  finally:
    conn.close()

  return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete-seller/<int:seller_id>', methods=['POST'])
@master_admin_required
def delete_seller(seller_id):
  reason_message = request.form.get('warning_message', '').strip() or (
      'Your seller account was removed by Admin due to misleading activity.'
  )

  conn = get_db()
  seller = conn.execute(
      "SELECT * FROM users WHERE id = ? AND role = 'seller'", (seller_id,)
  ).fetchone()
  if not seller or seller['email'] == 'sell1':
    conn.close()
    flash('Seller account not found.', 'danger')
    return redirect(url_for('admin_dashboard'))

  conn.execute(
      """INSERT INTO seller_notices (seller_id, seller_identifier, sent_by, message)
           VALUES (?, ?, ?, ?)""",
      (
          seller['id'],
          seller['email'],
          session.get('user_email', 'sell1'),
          reason_message,
      ),
  )
  conn.execute('DELETE FROM products WHERE seller_id = ?', (seller_id,))
  conn.execute('DELETE FROM users WHERE id = ?', (seller_id,))
  conn.commit()
  conn.close()

  flash(
      f"Seller '{seller['email']}' deleted and warning message recorded.",
      'info',
  )
  return redirect(url_for('admin_dashboard'))


@app.route('/admin/create-help-center', methods=['POST'])
@master_admin_required
def create_help_center_user():
  name = request.form['hc_name'].strip()
  hc_username = request.form['hc_username'].strip()
  hc_password = request.form['hc_password']

  if not name or not hc_username or not hc_password:
    flash(
        'Please provide Name, User ID, and Password for the Help Center'
        ' account.',
        'warning',
    )
    return redirect(url_for('admin_dashboard'))

  conn = get_db()
  try:
    conn.execute(
        """INSERT INTO users (name, email, password, role)
               VALUES (?, ?, ?, 'helpdesk')""",
        (name, hc_username, generate_password_hash(hc_password)),
    )
    conn.commit()
    flash(f"Help Center User ID '{hc_username}' created.", 'success')
  except sqlite3.IntegrityError:
    flash(f"User ID '{hc_username}' already exists.", 'danger')
  finally:
    conn.close()

  return redirect(url_for('admin_dashboard'))


@app.route('/admin/edit-help-center/<int:hc_id>', methods=['POST'])
@master_admin_required
def edit_help_center_user(hc_id):
  name = request.form.get('hc_name', '').strip()
  hc_username = request.form.get('hc_username', '').strip()
  hc_password = request.form.get('hc_password', '').strip()

  if not name or not hc_username:
    flash('Help Center name and user ID are required.', 'warning')
    return redirect(url_for('admin_dashboard'))

  conn = get_db()
  hc_user = conn.execute(
      "SELECT * FROM users WHERE id = ? AND role = 'helpdesk'", (hc_id,)
  ).fetchone()
  if not hc_user:
    conn.close()
    flash('Help Center account not found.', 'danger')
    return redirect(url_for('admin_dashboard'))

  try:
    if hc_password:
      conn.execute(
          """UPDATE users SET name = ?, email = ?, password = ?
                   WHERE id = ? AND role = 'helpdesk'""",
          (name, hc_username, generate_password_hash(hc_password), hc_id),
      )
    else:
      conn.execute(
          """UPDATE users SET name = ?, email = ?
                   WHERE id = ? AND role = 'helpdesk'""",
          (name, hc_username, hc_id),
      )
    conn.commit()
    flash(
        f"Help Center user ID and password updated for '{hc_username}'.",
        'success',
    )
  except sqlite3.IntegrityError:
    flash(
        f"User ID '{hc_username}' is already taken by another account.", 'danger'
    )
  finally:
    conn.close()

  return redirect(url_for('admin_dashboard'))


@app.route('/admin/complaint/<int:complaint_id>/manage', methods=['POST'])
@master_admin_required
def admin_manage_complaint(complaint_id):
  admin_solution = request.form.get('admin_solution', '').strip()
  seller_notice = request.form.get('seller_notice', '').strip()
  approve_refund = request.form.get('approve_refund') == 'on'
  verify_refund_proof = request.form.get('verify_refund_proof') == 'on'
  status = request.form.get('status', 'Resolved')

  if status not in COMPLAINT_STATUSES:
    status = 'Resolved'

  conn = get_db()
  comp = conn.execute(
      'SELECT * FROM complaints WHERE id = ?', (complaint_id,)
  ).fetchone()
  if comp:
    final_status = status
    target_seller_id = _resolve_complaint_seller_id(conn, comp)
    refund_requested = approve_refund or (status == 'Awaiting Seller Refund Proof')
    refund_approved = (
        1 if (refund_requested or comp['refund_approved']) else 0
    )
    refund_verified = (
        1 if (verify_refund_proof or comp['refund_verified']) else 0
    )

    if refund_requested and not comp['seller_refund_txn_id']:
      final_status = 'Awaiting Seller Refund Proof'
      if comp['order_id']:
        conn.execute(
            """UPDATE orders
                       SET payment_status = 'Refund Pending Seller Proof', status = 'Cancelled'
                       WHERE id = ?""",
            (comp['order_id'],),
        )
        items = conn.execute(
            'SELECT product_id, quantity FROM order_items WHERE order_id = ?',
            (comp['order_id'],),
        ).fetchall()
        for item in items:
          if item['product_id'] is not None:
            conn.execute(
                'UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?',
                (item['quantity'], item['product_id']),
            )

      req_msg = seller_notice or (
          f'Refund approved by Admin for Complaint #{complaint_id}. Please'
          ' submit Refund UPI / Transaction ID for verification.'
      )
      conn.execute(
          """INSERT INTO seller_notices (seller_id, seller_identifier, sent_by, message)
                   VALUES (?, ?, ?, ?)""",
          (
              target_seller_id,
              f'Seller #{target_seller_id}'
              if target_seller_id
              else 'All Sellers',
              'sell1 (Admin)',
              req_msg,
          ),
      )
      if not seller_notice:
        seller_notice = req_msg

    if verify_refund_proof:
      if not comp['seller_refund_txn_id']:
        conn.close()
        flash(
            'Seller must submit their Refund UPI / Transaction ID before'
            ' verification.',
            'warning',
        )
        return redirect(url_for('admin_dashboard'))
      refund_verified = 1
      final_status = 'Closed'
      if comp['order_id']:
        conn.execute(
            """UPDATE orders
                       SET payment_status = 'Refunded', status = 'Cancelled'
                       WHERE id = ?""",
            (comp['order_id'],),
        )

    if final_status == 'Closed':
      conn.execute(
          """UPDATE complaints
                   SET admin_solution = COALESCE(NULLIF(?, ''), admin_solution),
                       seller_notice = COALESCE(NULLIF(?, ''), seller_notice),
                       refund_approved = ?,
                       refund_verified = ?,
                       status = 'Closed',
                       closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP)
                   WHERE id = ?""",
          (
              admin_solution,
              seller_notice,
              refund_approved,
              refund_verified,
              complaint_id,
          ),
      )
      conn.commit()
      conn.close()
      flash(
          f'Complaint #{complaint_id} is now Closed and will be auto-deleted in'
          f' {AUTO_DELETE_CLOSED_DAYS} days.',
          'success',
      )
      return redirect(url_for('admin_dashboard'))

    conn.execute(
        """UPDATE complaints
               SET admin_solution = COALESCE(NULLIF(?, ''), admin_solution),
                   seller_notice = COALESCE(NULLIF(?, ''), seller_notice),
                   refund_approved = ?,
                   refund_verified = ?,
                   closed_at = NULL,
                   status = ?
               WHERE id = ?""",
        (
            admin_solution,
            seller_notice,
            refund_approved,
            refund_verified,
            final_status,
            complaint_id,
        ),
    )

    if seller_notice and not refund_requested and target_seller_id:
      conn.execute(
          """INSERT INTO seller_notices (seller_id, seller_identifier, sent_by, message)
                   VALUES (?, ?, ?, ?)""",
          (
              target_seller_id,
              f'Seller #{target_seller_id}',
              'sell1 (Admin)',
              seller_notice,
          ),
      )

    conn.commit()
    flash(f'Admin decision saved for Complaint #{complaint_id}.', 'success')
  conn.close()

  return redirect(url_for('admin_dashboard'))


# =========================================
# 11. Product & Order Actions
# =========================================
@app.route('/seller/add-product', methods=['POST'])
@seller_required
def add_product():
  conn = get_db()
  seller = conn.execute(
      'SELECT qr_code FROM users WHERE id = ?', (session['user_id'],)
  ).fetchone()
  if not seller or not seller['qr_code']:
    conn.close()
    flash(
        'Please upload your Online Payment QR Scanner before adding products.',
        'danger',
    )
    return redirect(get_user_dashboard_url())

  name = request.form['name'].strip()
  category = request.form['category'].strip()
  price = float(request.form['price'])
  stock_qty = max(0, int(request.form.get('stock_qty', 10)))
  description = request.form.get('description', '').strip()

  image_filename = 'placeholder.png'
  image_file = request.files.get('image')
  if image_file and image_file.filename:
    safe_name = secure_filename(image_file.filename)
    image_file.save(os.path.join(UPLOAD_FOLDER, safe_name))
    image_filename = safe_name

  conn.execute(
      """INSERT INTO products (seller_id, name, category, price, stock_qty, description, image)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
      (
          session['user_id'],
          name,
          category,
          price,
          stock_qty,
          description,
          image_filename,
      ),
  )
  conn.commit()
  conn.close()

  flash(f"Product '{name}' added.", 'success')
  return redirect(get_user_dashboard_url())


@app.route('/seller/update-stock/<int:product_id>', methods=['POST'])
@seller_required
def update_stock(product_id):
  new_stock = max(0, int(request.form.get('stock_qty', 0)))
  conn = get_db()
  if session.get('user_email') == 'sell1':
    conn.execute(
        'UPDATE products SET stock_qty = ? WHERE id = ?',
        (new_stock, product_id),
    )
  else:
    conn.execute(
        'UPDATE products SET stock_qty = ? WHERE id = ? AND seller_id = ?',
        (new_stock, product_id, session['user_id']),
    )
  conn.commit()
  conn.close()

  flash(f'Stock updated to {new_stock}.', 'success')
  return redirect(get_user_dashboard_url())


@app.route('/seller/delete-product/<int:product_id>', methods=['POST'])
@seller_required
def delete_product(product_id):
  conn = get_db()
  if session.get('user_email') == 'sell1':
    conn.execute('DELETE FROM products WHERE id = ?', (product_id,))
  else:
    conn.execute(
        'DELETE FROM products WHERE id = ? AND seller_id = ?',
        (product_id, session['user_id']),
    )
  conn.commit()
  conn.close()

  flash('Product deleted.', 'info')
  return redirect(get_user_dashboard_url())


@app.route('/seller/update-status/<int:order_id>', methods=['POST'])
@seller_required
def update_order_status(order_id):
  new_status = request.form.get('status')
  new_payment_status = request.form.get('payment_status')

  conn = get_db()
  current_order = conn.execute(
      'SELECT status, payment_status FROM orders WHERE id = ?', (order_id,)
  ).fetchone()
  if current_order:
    if new_status == 'Cancelled' and current_order['status'] != 'Cancelled':
      items = conn.execute(
          'SELECT product_id, quantity FROM order_items WHERE order_id = ?',
          (order_id,),
      ).fetchall()
      for item in items:
        if item['product_id'] is not None:
          conn.execute(
              'UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?',
              (item['quantity'], item['product_id']),
          )

    if new_status in ORDER_STATUSES:
      conn.execute(
          'UPDATE orders SET status = ? WHERE id = ?', (new_status, order_id)
      )
    if new_payment_status in PAYMENT_STATUSES:
      conn.execute(
          'UPDATE orders SET payment_status = ? WHERE id = ?',
          (new_payment_status, order_id),
      )
    conn.commit()
    flash(f'Order #{order_id} updated.', 'success')
  conn.close()

  return redirect(get_user_dashboard_url())


init_db()

if __name__ == '__main__':
  app.run(debug=True)
