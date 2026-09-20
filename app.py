import os
import json
import time
import threading
import traceback
import requests
from datetime import datetime
from flask import Flask, Response, render_template_string, request, jsonify

CONFIG_FILE = "config.json"
CACHE_FILE = "cache_items.json"
X2POS_HOST = os.getenv("X2POS_HOST", "https://x2pos.com")

DEFAULT_CONFIG = {
    "x2_user": os.getenv("X2POS_USER", "aiberasting@gmail.com"),
    "x2_pass": os.getenv("X2POS_PASS", "Pavelo31"),
    "exclude_zero_stock": True,
    "merchant_id": "30210258",
    "company_name": "30210258",
    "city_ids": "750000000, 195220100",  # Коды городов через запятую (750000000 - Алматы)
    "kaspi_warehouses": [
        {"point_id": "30210258_PP1", "name": "Склад Алматы (PP1)", "ratio_percent": 60},
        {"point_id": "30210258_QASQELEN", "name": "Склад Каскелен", "ratio_percent": 40}
    ],
    "products_override": {}  # sku: {"enabled": bool}
}

app = Flask(__name__)
sync_lock = threading.Lock()
is_syncing = False
sync_status_message = "Готов к работе"

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

def escape_xml(text):
    if text is None:
        return ""
    text = str(text)
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&apos;"))

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

    def get_company_branches(self):
        url = f"{self.host}/api/company_settings"
        resp = requests.get(url, headers=self._headers(), timeout=20)
        if resp.status_code in (401, 403):
            self.auth()
            resp = requests.get(url, headers=self._headers(), timeout=20)
        resp.raise_for_status()
        data = resp.json()
        
        branch_ids = []
        if isinstance(data, list) and len(data) > 0:
            branches_dict = data[0].get("branches", {})
            if isinstance(branches_dict, dict):
                branch_ids = list(branches_dict.keys())
        return branch_ids

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

    def get_stock(self, branch_id):
        url = f"{self.host}/api/stock?branch_id={branch_id}"
        resp = requests.get(url, headers=self._headers(), timeout=20)
        resp.raise_for_status()
        return resp.json()

# ================= СБОРКА И ОБНОВЛЕНИЕ ДАННЫХ =================
def run_full_sync():
    global is_syncing, sync_status_message
    if not sync_lock.acquire(blocking=False):
        return
    try:
        is_syncing = True
        sync_status_message = "Идет синхронизация с X2pos..."
        cfg = load_config()
        client = X2PosClient(X2POS_HOST, cfg["x2_user"], cfg["x2_pass"])
        client.auth()

        branch_ids = client.get_company_branches()
        print(f"[SYNC] Филиалы X2pos: {branch_ids}")

        stock_map = {}
        for b_id in branch_ids:
            try:
                raw_stock = client.get_stock(b_id)
                if isinstance(raw_stock, dict):
                    for k, val in raw_stock.items():
                        if isinstance(val, dict):
                            v_id = str(val.get("variation_id") or val.get("variation id") or k).strip()
                            qty = float(val.get("quantity") or 0)
                            stock_map[v_id] = stock_map.get(v_id, 0.0) + qty
                elif isinstance(raw_stock, list):
                    for row in raw_stock:
                        if isinstance(row, dict):
                            v_id = str(row.get("variation_id") or row.get("variation id") or "").strip()
                            if v_id:
                                qty = float(row.get("quantity") or 0)
                                stock_map[v_id] = stock_map.get(v_id, 0.0) + qty
            except Exception as e:
                print(f"[SYNC WARNING] Ошибка остатков филиала {b_id}: {e}")

        raw_products = client.get_products()
        items = []
        overrides = cfg.get("products_override", {})

        for prod in raw_products:
            if not isinstance(prod, dict) or prod.get("is_service") == "1":
                continue

            parent_sku = (prod.get("product_vendor_code") or "").strip()
            brand = (prod.get("product_brand") or "Generic").strip()
            variations = prod.get("variations") or []
            if isinstance(variations, dict):
                variations = list(variations.values())
            elif not isinstance(variations, list):
                variations = []

            for var in variations:
                if not isinstance(var, dict):
                    continue
                var_id = str(var.get("id") or "").strip()
                sku = (var.get("vendor_code") or parent_sku).strip()

                if not sku:
                    continue

                name = prod.get("product_name") or ""
                var_name = var.get("name")
                if var_name and str(var_name).lower() != "generic":
                    name = f"{name} ({var_name})"

                price = float(var.get("retail_price") or 0.0)
                raw_qty = stock_map.get(var_id, 0.0)
                qty = max(0, int(raw_qty))

                is_enabled = overrides.get(sku, {}).get("enabled", True)

                items.append({
                    "sku": sku,
                    "var_id": var_id,
                    "name": name,
                    "brand": brand,
                    "price": price,
                    "stock": qty,
                    "enabled": is_enabled
                })

        items.sort(key=lambda x: x["sku"])
        save_cached_items(items)
        sync_status_message = f"Успешно синхронизировано ({len(items)} товаров)"
        print(f"[SYNC SUCCESS] Загружено товаров: {len(items)}")
    except Exception as e:
        sync_status_message = f"Ошибка синхронизации: {str(e)}"
        print(f"[SYNC ERROR] {e}")
        traceback.print_exc()
    finally:
        is_syncing = False
        sync_lock.release()

# ================= ГЕНЕРАЦИЯ KASPI XML =================
def build_kaspi_xml():
    cfg = load_config()
    items = load_cached_items()
    
    if not items:
        run_full_sync()
        items = load_cached_items()

    warehouses = cfg.get("kaspi_warehouses", [])
    exclude_zero = cfg.get("exclude_zero_stock", False)

    company = escape_xml(cfg.get("company_name", "30210258"))
    merchantid = escape_xml(cfg.get("merchant_id", "30210258"))
    
    raw_cities = cfg.get("city_ids", "750000000")
    city_list = [c.strip() for c in raw_cities.split(",") if c.strip()]
    if not city_list:
        city_list = ["750000000"]

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<kaspi_catalog xmlns="kaspiShopping" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://kaspi.kz/kaspishopping.xsd" date="{now_str}">',
        f'    <company>{company}</company>',
        f'    <merchantid>{merchantid}</merchantid>',
        '    <offers>'
    ]

    for it in items:
        if not it.get("enabled", True):
            continue

        total_stock = it.get("stock", 0)
        if exclude_zero and total_stock <= 0:
            continue

        sku = escape_xml(it["sku"])
        name = escape_xml(it["name"])
        brand = escape_xml(it.get("brand") or "Generic")
        price = int(it.get("price", 0))

        xml_lines.append(f'        <offer sku="{sku}">')
        xml_lines.append(f'            <model>{name}</model>')
        xml_lines.append(f'            <brand>{brand}</brand>')
        xml_lines.append('            <availabilities>')

        for wh in warehouses:
            ratio = float(wh.get("ratio_percent", 0)) / 100.0
            point_id = escape_xml(str(wh.get("point_id", "PP1")).strip())
            allocated_qty = int(total_stock * ratio)

            if allocated_qty > 0:
                xml_lines.append(f'                <availability available="yes" storeId="{point_id}" preOrder="0" stockCount="{allocated_qty}.0"/>')
            else:
                xml_lines.append(f'                <availability available="no" storeId="{point_id}" preOrder="0"/>')

        xml_lines.append('            </availabilities>')
        xml_lines.append('            <cityprices>')
        for cid in city_list:
            cid_escaped = escape_xml(cid)
            xml_lines.append(f'                <cityprice cityId="{cid_escaped}">{price}</cityprice>')
        xml_lines.append('            </cityprices>')
        xml_lines.append('        </offer>')

    xml_lines.append('    </offers>')
    xml_lines.append('</kaspi_catalog>')

    return "\n".join(xml_lines)

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
            --success-hover: #059669;
            --info: #0284c7;
            --info-hover: #0369a1;
            --danger: #ef4444;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: system-ui, -apple-system, sans-serif; }
        body { background: var(--bg); color: var(--text); padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
        
        header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 16px; }
        h1 { font-size: 22px; color: var(--accent); }
        .btn-group { display: flex; gap: 10px; align-items: center; }
        
        .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
        .card h2 { font-size: 16px; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; }
        
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        label { display: block; font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
        input[type="text"], input[type="password"], input[type="number"] {
            width: 100%; background: #1f2937; border: 1px solid var(--border); color: #fff; border-radius: 6px; padding: 8px 10px; font-size: 13px; outline: none;
        }
        input:focus { border-color: var(--accent); }
        
        .wh-item { display: grid; grid-template-columns: 160px 1fr 90px 40px; gap: 10px; align-items: center; margin-bottom: 10px; background: #182234; padding: 10px; border-radius: 6px; }
        .btn-del { color: var(--danger); background: none; border: none; font-size: 18px; cursor: pointer; }
        
        .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; text-decoration: none; }
        .btn-success { background: var(--success); color: white; }
        .btn-success:hover { background: var(--success-hover); }
        .btn-primary { background: var(--accent); color: white; }
        .btn-primary:hover { background: var(--accent-hover); }
        .btn-info { background: var(--info); color: white; }
        .btn-info:hover { background: var(--info-hover); }
        .btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text); }
        .btn-outline:hover { background: var(--border); }
        
        .feed-box { display: flex; justify-content: space-between; align-items: center; background: #070a11; border: 1px dashed #374151; padding: 12px 16px; border-radius: 6px; margin-top: 10px; }
        .feed-box a.feed-url { color: #38bdf8; text-decoration: none; font-family: monospace; font-size: 13px; word-break: break-all; }
        
        .table-container { max-height: 480px; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px; margin-top: 12px; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }
        th { background: #182234; padding: 10px; position: sticky; top: 0; border-bottom: 1px solid var(--border); color: var(--text-muted); }
        td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
        
        .checkbox-row { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-top: 10px; cursor: pointer; }
        .checkbox-row input { cursor: pointer; width: 16px; height: 16px; }
        #statusLabel { font-size: 12px; color: #38bdf8; margin-right: 8px; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <div>
            <h1>Синхронизация X2POS ➔ Kaspi (kaspiShopping XML)</h1>
            <div style="color: var(--text-muted); font-size: 12px;">Строгая схема kaspishopping.xsd, склады, цены по городам и остатки</div>
        </div>
        <div class="btn-group">
            <span id="statusLabel">{{ status_msg }}</span>
            <button class="btn btn-primary" id="syncBtn" onclick="triggerSync()">🔄 Обновить из X2pos</button>
            <button class="btn btn-success" onclick="saveAll()">💾 Сохранить настройки</button>
        </div>
    </header>

    <div class="card" style="border-color: #38bdf8;">
        <h2>🔗 Ссылка на фид и скачивание файла</h2>
        <div class="feed-box">
            <a id="feedLink" class="feed-url" href="/kaspi-feed.xml" target="_blank">Загрузка...</a>
            <a href="/download-xml" class="btn btn-info" style="margin-left: 12px; white-space: nowrap;">📥 Скачать XML</a>
        </div>
        <div style="font-size: 12px; color: var(--text-muted); margin-top: 8px;">
            Вставьте эту ссылку в кабинете Kaspi: <b>Товары ➔ Загрузка прайс-листа</b>.
        </div>
    </div>

    <div class="grid-2">
        <div class="card">
            <h2>🏢 Магазин Kaspi & Города</h2>
            <div style="margin-bottom: 10px;">
                <label>Merchant ID / Company (ID продавца Kaspi)</label>
                <input type="text" id="merchantId" value="{{ config.merchant_id }}">
            </div>
            <div style="margin-bottom: 10px;">
                <label>ID Городов присутствия (через запятую)</label>
                <input type="text" id="cityIds" value="{{ config.city_ids }}" placeholder="750000000, 195220100">
                <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">
                    750000000 — Алматы, 195220100 — Каскелен, 710000000 — Астана
                </div>
            </div>
            
            <label class="checkbox-row" style="margin-top: 14px;">
                <input type="checkbox" id="excludeZero" {% if config.exclude_zero_stock %}checked{% endif %}>
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
                * storeId указывать строго как в Kaspi (например: <b>30210258_PP1</b>, <b>30210258_QASQELEN</b>, <b>30210258_NOEXPRESS</b>).
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
                        <th width="160">Артикул (SKU)</th>
                        <th>Наименование</th>
                        <th width="110">Бренд</th>
                        <th width="100">Цена</th>
                        <th width="90">Остаток X2</th>
                        <th width="110">Статус</th>
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
    }

    function renderWarehouses() {
        const box = document.getElementById("whContainer");
        box.innerHTML = "";
        cfg.kaspi_warehouses.forEach((wh, idx) => {
            const div = document.createElement("div");
            div.className = "wh-item";
            div.innerHTML = `
                <input type="text" class="wh-point-id" placeholder="storeId" value="${wh.point_id || ''}" oninput="cfg.kaspi_warehouses[${idx}].point_id=this.value">
                <input type="text" class="wh-name" placeholder="Название склада" value="${wh.name || ''}" oninput="cfg.kaspi_warehouses[${idx}].name=this.value">
                <input type="number" class="wh-ratio" min="0" max="100" placeholder="%" value="${wh.ratio_percent}" oninput="cfg.kaspi_warehouses[${idx}].ratio_percent=Number(this.value)">
                <button class="btn-del" onclick="deleteWarehouse(${idx})">&times;</button>
            `;
            box.appendChild(div);
        });
    }

    function addWarehouse() {
        // Синхронизируем текущее состояние перед добавлением
        collectWarehousesFromDOM();
        const prefix = document.getElementById("merchantId").value ? (document.getElementById("merchantId").value + "_") : "";
        cfg.kaspi_warehouses.push({ point_id: prefix + "PP" + (cfg.kaspi_warehouses.length + 1), name: "Новый склад", ratio_percent: 50 });
        renderWarehouses();
    }

    function deleteWarehouse(idx) {
        collectWarehousesFromDOM();
        cfg.kaspi_warehouses.splice(idx, 1);
        renderWarehouses();
    }

    function collectWarehousesFromDOM() {
        const items = document.querySelectorAll("#whContainer .wh-item");
        const newWarehouses = [];
        items.forEach(el => {
            const pId = el.querySelector(".wh-point-id").value.trim();
            const name = el.querySelector(".wh-name").value.trim();
            const ratio = Number(el.querySelector(".wh-ratio").value) || 0;
            newWarehouses.push({ point_id: pId, name: name, ratio_percent: ratio });
        });
        cfg.kaspi_warehouses = newWarehouses;
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
                <td style="color: #9ca3af;">${p.brand || 'Generic'}</td>
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
        // Принудительно вычитываем актуальные данные из инпутов в объект cfg
        collectWarehousesFromDOM();
        cfg.merchant_id = document.getElementById("merchantId").value.trim();
        cfg.company_name = cfg.merchant_id;
        cfg.city_ids = document.getElementById("cityIds").value.trim();
        cfg.exclude_zero_stock = document.getElementById("excludeZero").checked;

        fetch("/api/save", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(cfg)
        })
        .then(r => r.json())
        .then(res => {
            if(res.status === "ok") {
                alert("Настройки успешно сохранены!");
                location.reload();
            } else {
                alert("Ошибка: " + res.message);
            }
        })
        .catch(err => {
            alert("Ошибка сети при сохранении: " + err);
        });
    }

    function triggerSync() {
        const btn = document.getElementById("syncBtn");
        const status = document.getElementById("statusLabel");
        btn.disabled = true;
        status.innerText = "Синхронизация запущена...";

        fetch("/api/sync", { method: "POST" })
        .then(r => r.json())
        .then(() => {
            const poll = setInterval(() => {
                fetch("/api/status")
                .then(r => r.json())
                .then(data => {
                    status.innerText = data.message;
                    if (!data.is_syncing) {
                        clearInterval(poll);
                        btn.disabled = false;
                        location.reload();
                    }
                });
            }, 2000);
        })
        .catch(err => {
            status.innerText = "Ошибка: " + err;
            btn.disabled = false;
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
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    items = load_cached_items()
    return render_template_string(
        HTML_TEMPLATE,
        config=cfg,
        config_json=json.dumps(cfg),
        products_json=json.dumps(items),
        status_msg=sync_status_message
    )

@app.route("/api/sync", methods=["POST"])
def sync_api():
    thread = threading.Thread(target=run_full_sync)
    thread.daemon = True
    thread.start()
    return jsonify({"status": "started"})

@app.route("/api/status", methods=["GET"])
def status_api():
    return jsonify({
        "is_syncing": is_syncing,
        "message": sync_status_message
    })

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
        return Response(f"<error>{escape_xml(str(e))}</error>", status=500, mimetype="application/xml")

@app.route("/download-xml", methods=["GET"])
def download_xml():
    try:
        xml_res = build_kaspi_xml()
        return Response(
            xml_res,
            mimetype="application/xml; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=kaspi-feed.xml"
            }
        )
    except Exception as e:
        return Response(f"<error>{escape_xml(str(e))}</error>", status=500, mimetype="application/xml")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
