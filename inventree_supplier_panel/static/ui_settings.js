function ensureStyles() {
    if (document.getElementById('suppliercart-settings-style')) {
        return;
    }

    const style = document.createElement('style');
    style.id = 'suppliercart-settings-style';
    style.textContent = `
    .suppliercart-settings-card {
      border: 1px solid #d6dde6;
      border-radius: 0.5rem;
      padding: 1rem;
      margin-top: 0.5rem;
      background: #ffffff;
    }

    .suppliercart-settings-list {
      margin-bottom: 1rem;
    }

    .suppliercart-settings-status td {
      vertical-align: middle;
    }

    .suppliercart-badge {
      display: inline-block;
      border-radius: 999px;
      padding: 0.125rem 0.6rem;
      font-size: 0.8rem;
      font-weight: 600;
      color: #ffffff;
    }

    .suppliercart-badge-ok {
      background: #2f9e44;
    }

    .suppliercart-badge-error {
      background: #c92a2a;
    }

    .suppliercart-mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace;
      word-break: break-all;
    }
  `;

    document.head.appendChild(style);
}

function statusBadge(state, message) {
    const cls = state === 'ok' ? 'suppliercart-badge-ok' : 'suppliercart-badge-error';
    return `<span class="suppliercart-badge ${cls}">${message || ''}</span>`;
}

function buildAuthorizeUrl(context) {
    const base = context?.oauth_authorize_url || 'https://api.digikey.com/v1/oauth2/authorize';

    const params = new URLSearchParams({
        response_type: 'code',
        client_id: context?.client_id || '',
        redirect_uri: context?.redirect_uri || ''
    });

    return `${base}?${params.toString()}`;
}

export function renderPluginSettings(target, data) {
    if (!target) {
        return;
    }

    ensureStyles();

    const context = data?.context || data?.context?.context || {};
    const docsUrl = context.docs_url || 'https://github.com/SergeoLacruz/inventree-supplier-panel';
    const authorizeUrl = buildAuthorizeUrl(context);
    const canCreateToken = Boolean(context.client_id) && Boolean(context.redirect_uri);

    target.innerHTML = `
    <div class="suppliercart-settings-card">
      <p><b>Setup</b></p>
      <ol class="suppliercart-settings-list">
        <li>Read the <a href="${docsUrl}" target="_blank" rel="noopener noreferrer">documentation on GitHub</a></li>
        <li>Enable the plugin</li>
        <li>Put all required keys into settings</li>
        <li>Create the Digikey token</li>
        <li>Remove shopping carts and lists regularly from your supplier accounts</li>
      </ol>

      <p><b>Status</b></p>
      <table class="table table-condensed suppliercart-settings-status">
        <tr>
          <td>Server Base URL</td>
          <td>${statusBadge(context.base_url_state, context.base_url_message)}</td>
        </tr>
        <tr>
          <td>Callback URL (add this to your Digikey account)</td>
          <td class="suppliercart-mono">${context.redirect_uri || ''}</td>
        </tr>
      </table>

      <button type="button" class="btn btn-dark" id="suppliercart-create-digikey-token" ${canCreateToken ? '' : 'disabled'}>
        Create Digikey Token
      </button>
    </div>
  `;

    const button = target.querySelector('#suppliercart-create-digikey-token');

    if (!button) {
        return;
    }

    button.addEventListener('click', () => {
        window.open(authorizeUrl, 'suppliercart_digikey_oauth', 'width=1000,height=800');
    });
}
