function ensureStyles() {
  if (document.getElementById('suppliercart-po-panel-style')) {
    return;
  }

  const style = document.createElement('style');
  style.id = 'suppliercart-po-panel-style';
  style.textContent = `
    .suppliercart-wheel {
      border: 5px solid #f3f3f3;
      border-top: 5px solid #3498db;
      border-radius: 50%;
      width: 30px;
      height: 30px;
      animation: suppliercart-spin 2s linear infinite;
      visibility: hidden;
      margin-top: 0.5rem;
      margin-bottom: 0.5rem;
    }

    @keyframes suppliercart-spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }

    .suppliercart-align-right-6 th:nth-child(6),
    .suppliercart-align-right-6 td:nth-child(6),
    .suppliercart-align-right-7 th:nth-child(7),
    .suppliercart-align-right-7 td:nth-child(7) {
      text-align: right;
    }
  `;

  document.head.appendChild(style);
}

function formatNumber(value) {
  const numberValue = Number(value);

  if (Number.isFinite(numberValue)) {
    return numberValue.toFixed(4);
  }

  return value ?? '';
}

function showResult(resultNode, message) {
  resultNode.textContent = message;

  if (message === 'OK') {
    resultNode.className = 'alert alert-block alert-success';
  } else {
    resultNode.className = 'alert alert-block alert-danger';
  }
}

function renderTable(targetNode, cartData) {
  targetNode.innerHTML = '';

  if (!cartData || !Array.isArray(cartData.CartItems)) {
    return;
  }

  const table = document.createElement('table');
  table.className = 'table table-condensed suppliercart-align-right-6 suppliercart-align-right-7';

  const headers = [
    'IPN',
    'SKU',
    'Required',
    'Available',
    'Status',
    'Price',
    'Total',
    'Notes'
  ];

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');

  for (const headerText of headers) {
    const th = document.createElement('th');
    th.textContent = headerText;
    headRow.appendChild(th);
  }

  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');

  for (const item of cartData.CartItems) {
    const tr = document.createElement('tr');

    const cells = [
      item.IPN,
      item.SKU,
      item.QuantityRequested,
      item.QuantityAvailable,
      '',
      formatNumber(item.UnitPrice),
      formatNumber(item.ExtendedPrice),
      item.Error
    ];

    cells.forEach((cellValue, index) => {
      const td = document.createElement('td');

      if (index === 4) {
        td.classList.add('badge', 'badge-left', 'rounded-pill');
        if (Number(item.QuantityRequested) < Number(item.QuantityAvailable)) {
          td.classList.add('bg-success');
          td.textContent = 'OK';
        } else {
          td.classList.add('bg-danger');
          td.textContent = 'Not OK';
        }
      } else {
        td.textContent = cellValue ?? '';
      }

      tr.appendChild(td);
    });

    tbody.appendChild(tr);
  }

  table.appendChild(tbody);

  const tfoot = document.createElement('tfoot');
  const footRow = document.createElement('tr');
  const footer = [
    '',
    '',
    '',
    '',
    'Total',
    cartData.currency_code ?? '',
    formatNumber(cartData.MerchandiseTotal),
    ''
  ];

  for (const item of footer) {
    const td = document.createElement('td');
    td.textContent = item;
    td.style.textAlign = 'right';
    footRow.appendChild(td);
  }

  tfoot.appendChild(footRow);
  table.appendChild(tfoot);

  targetNode.appendChild(table);
}

export function renderSupplierCartPanel(target, data) {
  if (!target) {
    return;
  }

  ensureStyles();

  const panelContext = data?.context || {};

  target.innerHTML = `
    <button type="button" class="btn btn-dark" id="suppliercart-transfer-btn" title="Transfer PO to Supplier">
      <span class="fas fa-redo-alt"></span> Transfer PO
    </button>
    <div id="suppliercart-loader" class="suppliercart-wheel"></div>
    <div class="alert alert-block" id="suppliercart-result">&nbsp;</div>
    <b>Created supplier key:</b> <span id="suppliercart-cart-key"></span>
    <br>
    <b>Cart date:</b> <span id="suppliercart-cart-date"></span>
    <br>
    <div id="suppliercart-dynamic-table"></div>
  `;

  const transferButton = target.querySelector('#suppliercart-transfer-btn');
  const loader = target.querySelector('#suppliercart-loader');
  const result = target.querySelector('#suppliercart-result');
  const cartKey = target.querySelector('#suppliercart-cart-key');
  const cartDate = target.querySelector('#suppliercart-cart-date');
  const tableTarget = target.querySelector('#suppliercart-dynamic-table');

  function applyCartData(cartData) {
    if (!cartData) {
      return;
    }

    cartKey.textContent = cartData.cart_key || '';
    cartDate.textContent = cartData.cart_date || '';
    renderTable(tableTarget, cartData);
  }

  if (panelContext.cart) {
    applyCartData(panelContext.cart);
  }

  transferButton.addEventListener('click', async () => {
    if (!panelContext.transfer_url) {
      showResult(result, 'Transfer URL is missing');
      return;
    }

    loader.style.visibility = 'visible';

    try {
      const response = await fetch(panelContext.transfer_url, {
        method: 'GET',
        credentials: 'include'
      });

      const cartData = await response.json();

      showResult(result, cartData.message || 'Error');
      applyCartData(cartData);
    } catch (error) {
      showResult(result, 'Request failed');
    } finally {
      loader.style.visibility = 'hidden';
    }
  });
}
