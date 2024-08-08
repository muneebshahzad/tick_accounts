import asyncio
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request, flash, redirect, url_for, abort
import datetime, requests
from datetime import datetime
import pymssql, shopify
import aiohttp
import lazop
import aiohttp

app = Flask(__name__)
app.debug = True
app.secret_key = os.getenv('APP_SECRET_KEY', 'default_secret_key')  # Use environment variable

order_details = []
def get_db_connection():
    server = os.getenv('DB_SERVER')
    database = os.getenv('DB_DATABASE')
    username = os.getenv('DB_USERNAME')
    password = os.getenv('DB_PASSWORD')
    try:
        connection = pymssql.connect(server=server, user=username, password=password, database=database)
        return connection
    except pymssql.Error as e:
        print(f"Error connecting to the database: {str(e)}")
        return None

@app.route("/submit_tasks", methods=["POST"])
def submit_tasks():
    conn = get_db_connection()
    cursor = conn.cursor()

    today = datetime.date.today()
    cursor.execute("SELECT * FROM tasks WHERE Date = %s", (today,))
    existing_tasks = cursor.fetchone()

    if existing_tasks:
        flash("Tasks have already been submitted for today.", "error")
        return redirect(url_for("index"))

    selected_tasks = {}
    task_names = ["Confirm_Pending_Orders", "Track_Dispatched_Orders", "Contact_Abandoned_Orders", "Email_Courier",
                  "Track_Daraz_Orders", "Solve_Customer_Complaints", "Call_Delivered_Orders", "Add_Reviews_to_Shopify",
                  "Add_Reviews_to_Google_Maps", "Answer_Whatsapp_Messages_Calls", "Answer_Instagram_Facebook_Messages_Comments",
                  "Answer_Daraz_Messages", "Answer_Phone_Calls"]

    for task_name in task_names:
        selected_tasks[task_name] = 1 if task_name in request.form else 0

    cursor.execute("""
        INSERT INTO tasks (Date, Confirm_Pending_Orders, Track_Dispatched_Orders, Contact_Abandoned_Orders, Email_Courier,
        Track_Daraz_Orders, Solve_Customer_Complaints, Call_Delivered_Orders, Add_Reviews_to_Shopify,
        Add_Reviews_to_Google_Maps, Answer_Whatsapp_Messages_Calls, Answer_Instagram_Facebook_Messages_Comments,
        Answer_Daraz_Messages, Answer_Phone_Calls)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (today, *selected_tasks.values()))

    conn.commit()
    conn.close()

    flash("Tasks submitted successfully!", "success")
    return redirect(url_for("index"))

@app.route('/send-email', methods=['POST'])
def send_email():
    data = request.get_json()
    to_emails = data.get('to', [])
    cc_emails = data.get('cc', [])
    subject = data.get('subject', '')
    body = data.get('body', '')

    try:
        # SMTP server configuration
        smtp_server = 'smtp.gmail.com'
        smtp_port = 587
        smtp_user = os.getenv('SMTP_USER')  # Use environment variable
        smtp_password = os.getenv('SMTP_PASSWORD')  # Use environment variable

        # Create the message
        msg = MIMEText(body)
        msg['From'] = smtp_user
        msg['To'] = ', '.join(to_emails)
        msg['Cc'] = ', '.join(cc_emails)
        msg['Subject'] = subject

        # Connect to the SMTP server and send email
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_emails + cc_emails, msg.as_string())
        server.quit()

        return jsonify({'message': 'Email sent successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

async def fetch_tracking_data(session, tracking_number):

    api_key = os.getenv('LEOPARD_API_KEY')
    api_password = os.getenv('LEOPARD_PASSWORD')
    url = f"https://merchantapi.leopardscourier.com/api/trackBookedPacket/?api_key={api_key}&api_password={api_password}&track_numbers={tracking_number}"
    async with session.get(url) as response:
        return await response.json()

async def process_line_item(session, line_item, fulfillments):
    if line_item.fulfillment_status is None and line_item.fulfillable_quantity == 0:
        return []

    tracking_info = []

    if line_item.fulfillment_status == "fulfilled":
        for fulfillment in fulfillments:
            if fulfillment.status == "cancelled":
                continue
            for item in fulfillment.line_items:
                if item.id == line_item.id:
                    tracking_number = fulfillment.tracking_number
                    data = await fetch_tracking_data(session, tracking_number)
                    if data['status'] == 1 and not data['error']:
                        packet_list = data['packet_list']
                        if packet_list:
                            tracking_details = packet_list[0].get('Tracking Detail', [])
                            if tracking_details:
                                final_status = tracking_details[-1]['Status']
                                keywords = ["Return", "hold", "UNTRACEABLE"]
                                if not any(
                                        kw.lower() in final_status.lower() for kw in
                                        ["delivered", "returned to shipper"]):
                                    for detail in tracking_details:
                                        status = detail['Status']
                                        if status == 'Pending':
                                            reason = detail['Reason']
                                        else:
                                            reason = 'N/A'
                                        if any(kw in status for kw in keywords) or any(kw in reason for kw in keywords):
                                            final_status = "Being Return"
                                            break
                            else:
                                final_status = "Booked"
                                print("No tracking details available.")
                        else:
                            final_status = "Booked"
                            print("No packets found.")
                    else:
                        final_status = "N/A"
                        print("Error fetching data.")

                    # Track quantity for each tracking number
                    tracking_info.append({
                        'tracking_number': tracking_number,
                        'status': final_status,
                        'quantity': item.quantity
                    })

    return tracking_info if tracking_info else [
        {"tracking_number": "N/A", "status": "Un-Booked", "quantity": line_item.quantity}]

async def process_order(session, order):
    order_start_time = time.time()

    input_datetime_str = order.created_at
    parsed_datetime = datetime.fromisoformat(input_datetime_str[:-6])
    formatted_datetime = parsed_datetime.strftime("%b %d, %Y")

    try:
        status = (order.fulfillment_status).title()
    except:
        status = "Un-fulfilled"
    print(order)
    tags = []
    try:
        name = order.billing_address.name
    except AttributeError:
        name = " "
        print("Error retrieving name")

    try:
        address = order.billing_address.address1
    except AttributeError:
        address = " "
        print("Error retrieving address")

    try:
        city = order.billing_address.city
    except AttributeError:
        city = " "
        print("Error retrieving city")

    try:
        phone = order.billing_address.phone
    except AttributeError:
        phone = " "
        print("Error retrieving phone")

    customer_details = {
        "name": name,
        "address": address,
        "city": city,
        "phone": phone
    }
    order_info = {
        'order_id': order.order_number,
        'tracking_id': 'N/A',
        'created_at': formatted_datetime,
        'total_price': order.total_price,
        'line_items': [],
        'financial_status': (order.financial_status).title(),
        'fulfillment_status': status,
        'customer_details' : customer_details,
        'tags': order.tags.split(", "),
        'id': order.id
    }
    print(order.tags)

    tasks = []
    for line_item in order.line_items:
        tasks.append(process_line_item(session, line_item, order.fulfillments))

    results = await asyncio.gather(*tasks)
    variant_name = ""
    for tracking_info_list, line_item in zip(results, order.line_items):
        if tracking_info_list is None:
            continue

        if line_item.product_id is not None:
            product = shopify.Product.find(line_item.product_id)
            if product and product.variants:
                for variant in product.variants:
                    if variant.id == line_item.variant_id:
                        if variant.image_id is not None:
                            images = shopify.Image.find(image_id=variant.image_id, product_id=line_item.product_id)
                            variant_name = line_item.variant_title
                            for image in images:
                                if image.id == variant.image_id:
                                    image_src = image.src
                        else:
                            variant_name = ""
                            image_src = product.image.src
        else:
            image_src = "https://static.thenounproject.com/png/1578832-200.png"

        for info in tracking_info_list:
            order_info['line_items'].append({
                'fulfillment_status': line_item.fulfillment_status,
                'image_src': image_src,
                'product_title': line_item.title + " - " + variant_name ,
                'quantity': info['quantity'],
                'tracking_number': info['tracking_number'],
                'status': info['status']
            })
            order_info['status'] = info['status']

    order_end_time = time.time()
    print(f"Time taken to process order {order.order_number}: {order_end_time - order_start_time:.2f} seconds")

    return order_info



@app.route('/apply_tag', methods=['POST'])
def apply_tag():
    data = request.json
    order_id = data.get('order_id')
    tag = data.get('tag')

    # Get today's date in YYYY-MM-DD format
    today_date = datetime.now().strftime('%Y-%m-%d')
    tag_with_date = f"{tag} ({today_date})"

    try:
        order = shopify.Order.find(order_id)
        if order.tags:
            tags = order.tags.split(", ")
            # tags = tags.remove("Leopards Courier")
        else:
            tags = []
        if tag_with_date not in tags:
            tags.append(tag_with_date)
        order.tags = ", ".join(tags)
        order.save()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)})

async def getShopifyOrders():

    global order_details
    orders = shopify.Order.find(limit=250, order='created_at DESC')
    order_details = []
    total_start_time = time.time()

    async with aiohttp.ClientSession() as session:
        tasks = [process_order(session, order) for order in orders]
        order_details = await asyncio.gather(*tasks)

    total_end_time = time.time()
    print(f"Total time taken to process all orders: {total_end_time - total_start_time:.2f} seconds")

    return order_details

@app.route("/track")
def tracking():
    global order_details
    return render_template("track.html", order_details=order_details)

@app.route("/")
def index():
    today = datetime.now().date()

    conn = get_db_connection()
    cursor = conn.cursor(as_dict=True)

    cursor.execute("SELECT * FROM tasks WHERE Date = %s", (today,))
    tasks = cursor.fetchone()

    cursor.execute("SELECT Start FROM daily_sessions WHERE Date = %s", (today,))
    session_result = cursor.fetchone()

    conn.close()

    should_show_start_modal = "true" if not session_result else "false"
    should_show_resume_modal = "false" if should_show_start_modal == "true" else "true"

    start_time = session_result['Start'] if session_result else None

    return render_template("index.html", tasks=tasks, today=today,
                           should_show_start_modal=should_show_start_modal,
                           should_show_resume_modal=should_show_resume_modal,
                           start_time=start_time)

@app.route('/start_timer', methods=['POST'])
def start_timer():
    today = datetime.now().date()
    start_time = datetime.now().time()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO daily_sessions (Date, Start) VALUES (%s, %s)", (today, start_time))
    conn.commit()
    conn.close()

    return jsonify({"status": "started"})

@app.route('/resume_timer', methods=['POST'])
def resume_timer():
    today = datetime.datetime.now().date()
    resume_time = datetime.datetime.now().time()

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT MAX(Lap_Number) FROM timer_logs WHERE Date = %s", (today,))
    max_lap = cursor.fetchone()[0]
    new_lap_number = max_lap + 1 if max_lap is not None else 1

    cursor.execute("INSERT INTO timer_logs (Date, Lap_Number, Resume) VALUES (%s, %s, %s)",
                   (today, new_lap_number, resume_time))
    conn.commit()
    conn.close()

    return jsonify({"status": "resumed"})

@app.route('/get_elapsed_time', methods=['GET'])
def get_elapsed_time():
    today = datetime.now().date()

    conn = get_db_connection()
    cursor = conn.cursor(as_dict=True)
    cursor.execute("SELECT Start FROM daily_sessions WHERE Date = %s", (today,))
    session_result = cursor.fetchone()

    if session_result:
        start_time = session_result['Start']
        start_datetime = datetime.datetime.combine(today, start_time)
        current_datetime = datetime.datetime.now()

        cursor.execute("SELECT Resume FROM timer_logs WHERE Date = %s ORDER BY Lap_Number", (today,))
        laps = cursor.fetchall()
        total_pause_time = sum([(current_datetime - datetime.datetime.combine(today, lap['Resume'])).total_seconds() for lap in laps])

        elapsed_seconds = int((current_datetime - start_datetime).total_seconds() - total_pause_time)
    else:
        elapsed_seconds = 0

    conn.close()
    return jsonify({"elapsed_seconds": elapsed_seconds})


def get_daraz_orders(statuses):
    try:
        access_token = '50000601237osiZ0F1HkTZVojWcjq6szVDmDPjxiuvoEbCSvB15ff2bc8xtn4m'
        client = lazop.LazopClient('https://api.daraz.pk/rest', '501554', 'nrP3XFN7ChZL53cXyVED1yj4iGZZtlcD')

        all_orders = []

        for status in statuses:
            request = lazop.LazopRequest('/orders/get', 'GET')
            request.add_api_param('sort_direction', 'DESC')
            request.add_api_param('update_before', '2025-02-10T16:00:00+08:00')
            request.add_api_param('offset', '0')
            request.add_api_param('created_before', '2025-02-10T16:00:00+08:00')
            request.add_api_param('created_after', '2017-02-10T09:00:00+08:00')
            request.add_api_param('limit', '50')
            request.add_api_param('update_after', '2017-02-10T09:00:00+08:00')
            request.add_api_param('sort_by', 'updated_at')
            request.add_api_param('status', status)
            request.add_api_param('access_token', access_token)

            response = client.execute(request)
            darazOrders = response.body.get('data', {}).get('orders', [])

            for order in darazOrders:
                print(order)
                order_id = order.get('order_id', 'Unknown')

                item_request = lazop.LazopRequest('/order/items/get', 'GET')
                item_request.add_api_param('order_id', order_id)
                item_request.add_api_param('access_token', access_token)

                item_response = client.execute(item_request)
                try:
                    items = item_response.body.get('data', [])
                    if not items:
                        raise ValueError("No items found in the response.")
                except (AttributeError, ValueError) as e:
                    print(f"Error retrieving items: {e}")
                    items = []

                item_details = []
                for item in items:
                    tracking_num = item.get('tracking_code', 'Unknown')

                    tracking_req = lazop.LazopRequest('/logistic/order/trace', 'GET')
                    tracking_req.add_api_param('order_id', order_id)
                    tracking_req.add_api_param('access_token', access_token)
                    tracking_response = client.execute(tracking_req)

                    tracking_data = tracking_response.body.get('result', {})
                    packages = tracking_data.get('data', [{}])[0].get('package_detail_info_list', [])

                    track_status = "N/A"
                    for package in packages:
                        if package.get("tracking_number") == tracking_num:
                            try:
                                track_status = package.get('logistic_detail_info_list', [{}])[-1].get('title', "N/A")
                            except (IndexError, KeyError) as e:
                                print(f"Error processing tracking data: {e}")
                                track_status = "N/A"
                            print("MATCHED")
                            break

                    item_detail = {
                        'item_image': item.get('product_main_image', 'N/A'),
                        'item_title': item.get('name', 'Unknown'),
                        'quantity': item.get('variation', 'N/A'),
                        'tracking_number': item.get('tracking_code', 'N/A'),
                        'status': track_status
                    }
                    item_details.append(item_detail)

                filtered_order = {
                    'order_id': order.get('order_id', 'Unknown'),
                    'customer': {
                        'name': f"{order.get('customer_first_name', 'Unknown')} {order.get('customer_last_name', 'Unknown')}",
                        'address': order.get('address_shipping', {}).get('address', 'N/A'),
                        'phone': order.get('address_shipping', {}).get('phone', 'N/A')
                    },
                    'status': status.replace('_', ' ').title(),
                    'date': format_date(order.get('created_at', 'N/A')),
                    'total_price': order.get('price', '0.00'),
                    'items_list': item_details,
                }
                all_orders.append(filtered_order)

        return all_orders
    except Exception as e:
        print(f"Error fetching darazOrders: {e}")
        return []


@app.route('/daraz')
def daraz():
    statuses = ['shipped','pending','ready_to_ship']
    darazOrders = get_daraz_orders(statuses)
    return render_template('daraz.html', darazOrders=darazOrders)

def format_date(date_str):
    # Parse the date string
    date_obj = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S %z")
    # Format the date object to only show the date
    return date_obj.strftime("%Y-%m-%d")

@app.route('/refresh', methods=['POST'])
def refresh_data():
    global order_details
    try:
        order_details = asyncio.run(getShopifyOrders())
        return jsonify({'message': 'Data refreshed successfully'})
    except Exception as e:
        print(f"Error refreshing data: {e}")
        return jsonify({'message': 'Failed to refresh data'}), 500

def check_database_connection():
    server = 'tickbags.database.windows.net'
    database = 'TickBags'
    username = 'tickbags_ltd'
    password = 'TB@2024!'

    try:
        print('Connecting to the database...')
        connection = pymssql.connect(server=server, user=username, password=password, database=database)
        print('Connected to the database')
        return connection
    except pymssql.Error as e:
        print(f"Error connecting to the database: {str(e)}")
        return None

def fetch_transaction_data():
    connection = check_database_connection()
    if connection is None:
        return []
    print("CONNECTED TO DATABASE")

    try:
        with connection.cursor(as_dict=True) as cursor:
            query = 'SELECT * FROM transactiondetails3 ORDER BY Payment_Date desc'
            cursor.execute(query)
            transactions = cursor.fetchall()
            return transactions
    except pymssql.Error as e:
        print(f"Error fetching data from the database: {str(e)}")
        return []
    finally:
        connection.close()

@app.route('/finance_report')
def finance_report():
    transactions = fetch_transaction_data()

    return render_template('finance_report.html', transactions=transactions)

@app.route('/addTransaction')
def addTransaction():

    return render_template('addTransaction.html')

def run_async(func, *args, **kwargs):
    return asyncio.run(func(*args, **kwargs))
@app.route('/track/<tracking_num>')
def displayTracking(tracking_num):
    print(f"Tracking Number: {tracking_num}")  # Debug line

    async def async_func():
        async with aiohttp.ClientSession() as session:
            return await fetch_tracking_data(session, tracking_num)

    data = run_async(async_func)

    return render_template('trackingdata.html', data=data)


shop_url = os.getenv('SHOP_URL')
api_key = os.getenv('API_KEY')
password = os.getenv('PASSWORD')
shopify.ShopifyResource.set_site(shop_url)
shopify.ShopifyResource.set_user(api_key)
shopify.ShopifyResource.set_password(password)
order_details = asyncio.run(getShopifyOrders())

if __name__ == "__main__":
    shop_url = os.getenv('SHOP_URL')
    api_key = os.getenv('API_KEY')
    password = os.getenv('PASSWORD')
    shopify.ShopifyResource.set_site(shop_url)
    shopify.ShopifyResource.set_user(api_key)
    shopify.ShopifyResource.set_password(password)

    app.run(port=5001)


