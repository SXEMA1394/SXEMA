import os
import json
import time
import threading
import requests
import xml.etree.ElementTree as ET
from xml.dom import minidom
from flask import Flask, Response, render_template_string, request, jsonify

CONFIG_FILE = "config.json"
CACHE_FILE = "cache_items.json"
X2POS_HOST = os.getenv("X2POS_HOST", "https://x2pos.com")

DEFAULT_CONFIG = {
    "x2_user": os.getenv("X2POS_USER", "aiberasting@gmail.com"),
    "x2_pass": os.getenv("X2POS_PASS", "Pavelo31"),
    "exclude_zero_stock": True,
    "merchant_id": "YOUR_MERCHANT_ID",
    "company_name": "Kaspi Store",
    "kaspi_warehouses": [
        {"point_id": "PP1", "name": "Склад Алматы", "ratio_percent": 60},
        {"point_id": "PP2", "name": "Склад Регионы", "ratio_percent": 40}
    ],
    "products_override": {}  # sku: {"enabled": bool}
}

app = Flask(__name__)
sync_lock = threading.Lock()
is_syncing = False

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in cfg:
                        cfg[k] = v
                return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def load_cached_items():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_cached_items(items):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ================= КЛИЕНТ X2POS API =================
class X2PosClient:
    def __init__(self, host, user, password):
        self.host = host.rstrip('/')
        self.user = user
        self.password = password
        self.token = None

    def auth(self):
        url = f"{self.host}/api/auth"
        resp = requests.post(url, data={"user": self.user, "password": self.password}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        self.token = data.get("token")
        if not self.token:
            raise ValueError(f"Ошибка авторизации X2pos: {data}")
        return self.token

    def _headers(self):
        if not self.token:
            self.auth()
        return {"API-KEY": self.token}

    def get_products(self):
        products = []
        page = 1
        while True:
            url = f"{self.host}/api/products?page={page}"
            resp = requests.get(url, headers=self._headers(), timeout=25)
            if resp.status_code in (401, 403):
                self.auth()
                resp = requests.get(url, headers=self._headers(), timeout=25)
            resp.raise_for_status()
            data = resp.json()
            if not data or not isinstance(data, list):
                break
            products.extend(data)
            if len(data) < 50:
                break
            page += 1
        return products

    def get_stock(self, branch_id="all"):
        url = f"{self.host}/api/stock?branch_id={branch_id}"
        resp = requests.get(url, headers=self._headers(), timeout=20)
        resp.raise_for_status()
        return resp.json()

# ================= СБОРКА И ОБНОВЛЕНИЕ ДАННЫХ =================
def run_full_sync():
    """Фоновое обновление товаров и остатков из X2pos"""
    global is_syncing
    if not sync_lock.acquire(blocking=False):
        return
    try:
        is_syncing = True
        cfg = load_config()
        client = X2PosClient(X2POS_HOST, cfg["x2_user"], cfg["x2_pass"])
        client.auth()

        raw_products = client.get_products()
        stock_data = client.get_stock("all")

        items = []
        overrides = cfg.get("products_override", {})

        for prod in raw_products:
            if prod.get("is_service") == "1":
                continue

            parent_sku = (prod.get("product_vendor_code") or "").strip()
            variations = prod.get("variations", [])
            if isinstance(variations, dict):
                variations = list(variations.values())

            for var in variations:
                var_id = str(var.get("id"))
                sku = (var.get("vendor_code") or parent_sku).strip()

                # Учитываем только товары с артикулом
                if not sku:
                    continue

                name = prod.get("product_name", "")
                var_name = var.get("name")
                if var_name and var_name.lower() != "generic":
                    name = f"{name} ({var_name})"

                price = float(var.get("retail_price") or 0.0)
                raw_qty = stock_data.get(var_id, {}).get("quantity", 0)
                qty = max(0, int(float(raw_qty)))

                is_enabled = overrides.get(sku, {}).get("enabled", True)

                items.append({
                    "sku": sku,
                    "var_id": var_id,
                    "name": name,
                    "price": price,
                    "stock": qty,
                    "enabled": is_enabled
                })

        items.sort(key=lambda x: x["sku"])
        save_cached_items(items)
    except Exception as e:
        print(f"[SYNC ERROR] {e}")
    finally:
        is_syncing = False
        sync_lock.release()

def build_kaspi_xml():
    cfg = load_config()
    items = load_cached_items()
    
    # Если локальный кеш ещё пуст — запускаем синхронную первую загрузку
    if not items:
        run_full_sync()
        items = load_cached_items()

    warehouses = cfg.get("kaspi_warehouses", [])
    exclude_zero = cfg.get("exclude_zero_stock", False)

    root = ET.Element("kaspi_catalog", {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "xmlns": "kaspi_catalog"
    })
    
    company = ET.SubElement(root, "company")
    company.text = cfg.get("company_name", "Kaspi Store")
    merchantid = ET.SubElement(root, "merchantid")
    merchantid.text = cfg.get("merchant_id", "MERCHANT_ID")

    offers = ET.SubElement(root, "offers")

    for it in items:
        if not it.get("enabled", True):
            continue

        total_stock = it.get("stock", 0)
        if exclude_zero and total_stock <= 0:
            continue

        offer = ET.SubElement(offers, "offer", {"sku": it["sku"]})
        model = ET.SubElement(offer, "model")
        model.text = it["name"]
        brand = ET.SubElement(offer, "brand")
        brand.text = "Generic"
        price = ET.SubElement(offer, "price")
        price.text = str(int(it["price"]))

        availabilities = ET.SubElement(offer, "availabilities")
        for wh in warehouses:
            ratio = float(wh.get("ratio_percent", 0)) / 100.0
            point_id = wh.get("point_id", "PP1").strip()
            allocated_qty = int(total_stock * ratio)

            is_avail = "yes" if allocated_qty > 0 else "no"
            ET.SubElement(availabilities, "availability", {
                "available": is_avail,
                "storeId": point_id,
                "stock": str(allocated_qty)
            })

    rough_str = ET.tostring(root, 'utf-8')
    return minidom.parseString(rough_str).toprettyxml(indent="  ", encoding="utf-8")

# ================= ВЕБ-ИНТЕРФЕЙС =================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kaspi Stock Manager & XML Feed</title>
    <style>
        :root {
            --bg: #0b0f19;
            --panel: #111827;
            --border: #1f2937;
            --text: #f9fafb;
            --text-muted: #9ca3af;
            --accent: #f97316;
            --accent-hover: #ea580c;
            --success: #10b981;
            --danger: #ef4444;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: system-ui, -apple-system, sans-serif; }
        body { background: var(--bg); color: var(--text); padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
        
        header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 16px; }
        h1 { font-size: 22px; color: var(--accent); }
        .btn-group { display: flex; gap: 10px; }
        
        .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
        .card h2 { font-size: 16px; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; }
        
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        label { display: block; font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
        input[type="text"], input[type="password"], input[type="number"] {
            width: 100%; background: #1f2937; border: 1px solid var(--border); color: #fff; border-radius: 6px; padding: 8px 10px; font-size: 13px; outline: none;
        }
        input:focus { border-color: var(--accent); }
        
        .wh-item { display: grid; grid-template-columns: 140px 1fr 100px 40px; gap: 10px; align-items: center; margin-bottom: 10px; background: #182234; padding: 10px; border-radius: 6px; }
        .btn-del { color: var(--danger); background: none; border: none; font-size: 18px; cursor: pointer; }
        
        .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; }
        .btn-success { background: var(--success); color: white; }
        .btn-primary { background: var(--accent); color: white; }
        .btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text); }
        .btn-outline:hover { background: var(--border); }
        
        .feed-box { background: #070a11; border: 1px dashed #374151; padding: 12px; border-radius: 6px; margin-top: 10px; word-break: break-all; }
        .feed-box a { color: #38bdf8; text-decoration: none; font-family: monospace; font-size: 13px; }
        
        .table-container { max-height: 480px; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px; margin-top: 12px; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }
        th { background: #182234; padding: 10px; position: sticky; top: 0; border-bottom: 1px solid var(--border); color: var(--text-muted); }
        td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
        
        .checkbox-row { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-top: 10px; cursor: pointer; }
        .checkbox-row input { cursor: pointer; width: 16px; height: 16px; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <div>
            <h1>Синхронизация X2POS ➔ Склады Kaspi</h1>
            <div style="color: var(--text-muted); font-size: 12px;">Фильтрация по артикулу, управление долями складов Kaspi и XML-фид</div>
        </div>
        <div class="btn-group">
            <button class="btn btn-primary" onclick="triggerSync()">🔄 Обновить из X2pos</button>
            <button class="btn btn-success" onclick="saveAll()">💾 Сохранить настройки</button>
        </div>
    </header>

    <div class="card" style="border-color: #38bdf8;">
        <h2>🔗 Ваша ссылка на XML-фид для кабинета Kaspi</h2>
        <div class="feed-box">
            <a id="feedLink" href="/kaspi-feed.xml" target="_blank">Загрузка...</a>
        </div>
        <div style="font-size: 12px; color: var(--text-muted); margin-top: 6px;">
            Вставьте эту ссылку в кабинете Kaspi: <b>Товары ➔ Настройка загрузки прайс-листа</b>.
        </div>
    </div>

    <div class="grid-2">
        <div class="card">
            <h2>🏢 Магазин Kaspi & Фильтры</h2>
            <div style="margin-bottom: 10px;">
                <label>Merchant ID (ID продавца Kaspi)</label>
                <input type="text" id="merchantId" value="{{ config.merchant_id }}" onchange="cfg.merchant_id = this.value">
            </div>
            <div style="margin-bottom: 10px;">
                <label>Название магазина</label>
                <input type="text" id="companyName" value="{{ config.company_name }}" onchange="cfg.company_name = this.value">
            </div>
            
            <label class="checkbox-row" style="margin-top: 16px;">
                <input type="checkbox" id="excludeZero" {% if config.exclude_zero_stock %}checked{% endif %} onchange="cfg.exclude_zero_stock = this.checked">
                <span style="font-weight: 600; color: #fbbf24;">Снять с публикации товары с 0 остатком</span>
            </label>
        </div>

        <div class="card">
            <h2>
                <span>🏬 Склады Kaspi (storeId)</span>
                <button class="btn btn-outline" onclick="addWarehouse()">+ Добавить склад</button>
            </h2>
            <div id="whContainer"></div>
            <div style="font-size: 11px; color: var(--text-muted); margin-top: 8px;">
                * Point ID склада (например: <b>PP1</b>, <b>PP2</b>) берется из настроек Kaspi. Доли рассчитываются пропорционально.
            </div>
        </div>
    </div>

    <div class="card">
        <h2>📦 Товары с артикулом (<span id="prodCount">0</span>)</h2>
        <div style="display: flex; gap: 10px; margin-bottom: 10px;">
            <input type="text" id="search" placeholder="Поиск по артикулу..." onkeyup="filterRows()" style="max-width: 300px;">
            <button class="btn btn-outline" onclick="bulkToggle(true)">Включить все</button>
            <button class="btn btn-outline" onclick="bulkToggle(false)">Отключить все</button>
        </div>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th width="40"><input type="checkbox" onclick="bulkToggle(this.checked)"></th>
                        <th width="140">Артикул (SKU)</th>
                        <th>Наименование</th>
                        <th width="110">Цена</th>
                        <th width="90">Остаток X2</th>
                        <th width="120">Статус</th>
                    </tr>
                </thead>
                <tbody id="pBody"></tbody>
            </table>
        </div>
    </div>
</div>

<script>
    let cfg = {{ config_json | safe }};
    let products = {{ products_json | safe }};

    function initPage() {
        document.getElementById("feedLink").innerText = window.location.origin + "/kaspi-feed.xml";
        document.getElementById("feedLink").href = window.location.origin + "/kaspi-feed.xml";
        renderWarehouses();
        renderProducts();
        
        // Если при первом открытии список пуст, запускаем фоновую синхронизацию
        if (!products || products.length === 0) {
            triggerSync(true);
        }
    }

    function renderWarehouses() {
        const box = document.getElementById("whContainer");
        box.innerHTML = "";
        cfg.kaspi_warehouses.forEach((wh, idx) => {
            const div = document.createElement("div");
            div.className = "wh-item";
            div.innerHTML = `
                <input type="text" placeholder="Point ID (PP1)" value="${wh.point_id}" onchange="wh.point_id=this.value">
                <input type="text" placeholder="Название склада" value="${wh.name}" onchange="wh.name=this.value">
                <input type="number" min="0" max="100" placeholder="%" value="${wh.ratio_percent}" onchange="wh.ratio_percent=Number(this.value)">
                <button class="btn-del" onclick="deleteWarehouse(${idx})">&times;</button>
            `;
            box.appendChild(div);
        });
    }

    function addWarehouse() {
        cfg.kaspi_warehouses.push({ point_id: "PP" + (cfg.kaspi_warehouses.length + 1), name: "Новый склад", ratio_percent: 50 });
        renderWarehouses();
    }

    function deleteWarehouse(idx) {
        cfg.kaspi_warehouses.splice(idx, 1);
        renderWarehouses();
    }

    function renderProducts() {
        const tb = document.getElementById("pBody");
        tb.innerHTML = "";
        document.getElementById("prodCount").innerText = products.length;

        products.forEach(p => {
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td><input type="checkbox" class="p-cb" data-sku="${p.sku}" ${p.enabled ? 'checked' : ''} onchange="toggleItem('${p.sku}', this.checked)"></td>
                <td style="font-weight: 600; color: #38bdf8;">${p.sku}</td>
                <td>${p.name}</td>
                <td>${p.price.toLocaleString()} ₸</td>
                <td style="font-weight: 700; color: ${p.stock > 0 ? '#10b981' : '#ef4444'}">${p.stock} шт</td>
                <td><span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: ${p.enabled ? '#065f46' : '#7f1d1d'}">${p.enabled ? 'В фиде' : 'Выключен'}</span></td>
            `;
            tb.appendChild(tr);
        });
    }

    function toggleItem(sku, status) {
        if (!cfg.products_override) cfg.products_override = {};
        if (!cfg.products_override[sku]) cfg.products_override[sku] = {};
        cfg.products_override[sku].enabled = status;
        const it = products.find(x => x.sku === sku);
        if (it) it.enabled = status;
    }

    function bulkToggle(status) {
        document.querySelectorAll(".p-cb").forEach(cb => {
            cb.checked = status;
            toggleItem(cb.getAttribute("data-sku"), status);
        });
        renderProducts();
    }

    function filterRows() {
        const val = document.getElementById("search").value.toLowerCase();
        document.querySelectorAll("#pBody tr").forEach(r => {
            r.style.display = r.innerText.toLowerCase().includes(val) ? "" : "none";
        });
    }

    function saveAll() {
        fetch("/api/save", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(cfg)
        })
        .then(r => r.json())
        .then(res => {
            if(res.status === "ok") {
                alert("Настройки сохранены!");
            } else {
                alert("Ошибка: " + res.message);
            }
        });
    }

    function triggerSync(silent=false) {
        if (!silent) alert("Синхронизация с X2pos запущена в фоне. Страница обновится через несколько секунд.");
        fetch("/api/sync", { method: "POST" })
        .then(r => r.json())
        .then(res => {
            setTimeout(() => { location.reload(); }, 4000);
        });
    }

    window.onload = initPage;
</script>
</body>
</html>
"""

# ================= ЭНДПОИНТЫ =================
@app.route("/healthz", methods=["GET"])
def health_check():
    """Мгновенный ответ для health check платформы Render"""
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    items = load_cached_items()
    return render_template_string(
        HTML_TEMPLATE,
        config=cfg,
        config_json=json.dumps(cfg),
        products_json=json.dumps(items)
    )

@app.route("/api/sync", methods=["POST"])
def sync_api():
    thread = threading.Thread(target=run_full_sync)
    thread.daemon = True
    thread.start()
    return jsonify({"status": "started"})

@app.route("/api/save", methods=["POST"])
def save_api():
    try:
        data = request.get_json()
        save_config(data)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route("/kaspi-feed.xml", methods=["GET"])
def feed():
    try:
        xml_res = build_kaspi_xml()
        return Response(xml_res, mimetype="application/xml; charset=utf-8")
    except Exception as e:
        return Response(f"<error>{str(e)}</error>", status=500, mimetype="application/xml")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
