const periods = [7, 14, 30, 60, 90];
const state = {
  period: Number(localStorage.getItem("ozon-period")) || 30,
  allProducts: localStorage.getItem("ozon-all-products") !== "false",
  selected: new Set(JSON.parse(localStorage.getItem("ozon-selected-skus") || "[]")),
  products: [],
};

const elements = {
  periods: document.querySelector("#periods"),
  allProducts: document.querySelector("#all-products"),
  search: document.querySelector("#search"),
  list: document.querySelector("#product-list"),
  count: document.querySelector("#selected-count"),
  status: document.querySelector("#catalog-status"),
  notice: document.querySelector("#notice"),
  send: document.querySelector("#send"),
};

function saveState() {
  localStorage.setItem("ozon-period", String(state.period));
  localStorage.setItem("ozon-all-products", String(state.allProducts));
  localStorage.setItem("ozon-selected-skus", JSON.stringify([...state.selected]));
}

function renderPeriods() {
  elements.periods.replaceChildren(...periods.map((days) => {
    const label = document.createElement("label");
    label.className = "period-option";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "period";
    input.value = String(days);
    input.checked = state.period === days;
    input.addEventListener("change", () => {
      state.period = days;
      saveState();
    });
    const caption = document.createElement("span");
    caption.textContent = `${days} дн.`;
    label.append(input, caption);
    return label;
  }));
}

function renderProducts() {
  const query = elements.search.value.trim().toLocaleLowerCase("ru");
  const visible = state.products.filter(({ name, sku }) =>
    !query || name.toLocaleLowerCase("ru").includes(query) || sku.toLocaleLowerCase("ru").includes(query)
  );

  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = state.products.length ? "Ничего не найдено" : "Каталог пока пуст";
    elements.list.replaceChildren(empty);
  } else {
    elements.list.replaceChildren(...visible.map((product) => {
      const label = document.createElement("label");
      label.className = `product${state.allProducts ? " disabled" : ""}`;
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = state.selected.has(product.sku);
      input.disabled = state.allProducts;
      input.addEventListener("change", () => {
        input.checked ? state.selected.add(product.sku) : state.selected.delete(product.sku);
        saveState();
        updateSummary();
      });
      const marker = document.createElement("span");
      marker.className = "product-marker";
      const copy = document.createElement("span");
      copy.className = "product-copy";
      const name = document.createElement("span");
      name.className = "product-name";
      name.textContent = product.name;
      const sku = document.createElement("span");
      sku.className = "product-sku";
      sku.textContent = `SKU: ${product.sku}`;
      copy.append(name, sku);
      label.append(input, marker, copy);
      return label;
    }));
  }
  updateSummary();
}

function updateSummary() {
  const selectedCount = state.selected.size;
  elements.count.textContent = state.allProducts ? "Все" : `${selectedCount} выбрано`;
  elements.send.disabled = !state.products.length || (!state.allProducts && selectedCount === 0);
}

function encodeRequest() {
  const payload = JSON.stringify({
    p: state.period,
    s: state.allProducts ? "all" : [...state.selected],
  });
  const bytes = new TextEncoder().encode(payload);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function sendRequest() {
  elements.notice.textContent = "";
  const request = `#ozon_sales ${encodeRequest()}`;
  if (request.length > 3500) {
    elements.notice.textContent = "Выбрано слишком много товаров. Сократите список или выберите все номенклатуры.";
    return;
  }

  saveState();
  if (navigator.share) {
    try {
      await navigator.share({ text: request });
      return;
    } catch (error) {
      if (error.name === "AbortError") return;
    }
  }
  const shareUrl = new URL("https://t.me/share/url");
  shareUrl.searchParams.set("url", window.location.href);
  shareUrl.searchParams.set("text", request);
  window.location.href = shareUrl.toString();
}

async function loadCatalog() {
  try {
    const response = await fetch(`products.json?v=${Date.now()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const catalog = await response.json();
    state.products = Array.isArray(catalog.products) ? catalog.products : [];
    const available = new Set(state.products.map(({ sku }) => String(sku)));
    state.selected = new Set([...state.selected].filter((sku) => available.has(sku)));
    elements.status.textContent = catalog.updated_at
      ? `Обновлено ${new Date(catalog.updated_at).toLocaleString("ru-RU")}`
      : "Ожидается первое обновление";
    renderProducts();
  } catch (error) {
    elements.status.textContent = "Не удалось загрузить каталог";
    elements.notice.textContent = "Обновите страницу немного позже.";
  }
}

elements.allProducts.checked = state.allProducts;
elements.allProducts.addEventListener("change", () => {
  state.allProducts = elements.allProducts.checked;
  saveState();
  renderProducts();
});
elements.search.addEventListener("input", renderProducts);
elements.send.addEventListener("click", sendRequest);

renderPeriods();
loadCatalog();
