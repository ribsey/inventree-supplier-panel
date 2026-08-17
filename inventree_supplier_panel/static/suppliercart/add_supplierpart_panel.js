/**
 * Panel source for Part "Automatic Supplier parts" panel.
 * Fetches server-rendered HTML from the plugin's part-panel endpoint.
 */
export async function renderPanel(element, context) {
    const url = context.panel_url;
    if (!url) {
        element.innerHTML = '<p style="color:red">Panel URL missing from context</p>';
        return;
    }

    let html;
    try {
        const response = await fetch(url, { credentials: 'same-origin' });
        if (!response.ok) {
            element.innerHTML = `<p style="color:red">Failed to load panel (${response.status})</p>`;
            return;
        }
        html = await response.text();
    } catch (err) {
        element.innerHTML = `<p style="color:red">Network error: ${err.message}</p>`;
        return;
    }

    const container = document.createElement('div');
    container.innerHTML = html;

    element.appendChild(container);

    container.querySelectorAll('script').forEach(oldScript => {
        const newScript = document.createElement('script');
        Array.from(oldScript.attributes).forEach(a => newScript.setAttribute(a.name, a.value));
        newScript.textContent = oldScript.textContent;
        oldScript.parentNode.replaceChild(newScript, oldScript);
    });
}
