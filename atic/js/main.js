// ===================== THEME TOGGLE =====================
const themeToggle = document.getElementById('themeToggle');
const html = document.documentElement;

function getTheme() {
    return localStorage.getItem('theme') || 'dark';
}

function setTheme(t) {
    html.setAttribute('data-theme', t);
    localStorage.setItem('theme', t);
}

// Apply saved theme on load
setTheme(getTheme());

if (themeToggle) {
    themeToggle.addEventListener('click', () => {
        const current = html.getAttribute('data-theme');
        setTheme(current === 'dark' ? 'light' : 'dark');
    });
}

// ===================== MOBILE MENU =====================
const mobileMenuBtn = document.getElementById('mobileMenuBtn');
const mobileMenu = document.getElementById('mobileMenu');

if (mobileMenuBtn && mobileMenu) {
    mobileMenuBtn.addEventListener('click', () => {
        mobileMenu.classList.toggle('open');
        const spans = mobileMenuBtn.querySelectorAll('span');
        mobileMenu.classList.contains('open')
            ? spans[0].style.transform = 'rotate(45deg) translate(5px, 5px)'
            : spans[0].style.transform = '';
        mobileMenu.classList.contains('open')
            ? spans[1].style.opacity = '0'
            : spans[1].style.opacity = '';
        mobileMenu.classList.contains('open')
            ? spans[2].style.transform = 'rotate(-45deg) translate(5px, -5px)'
            : spans[2].style.transform = '';
    });
}

// ===================== PASTE BUTTON =====================
const pasteBtn = document.getElementById('pasteBtn');
const urlInput = document.getElementById('urlInput');

if (pasteBtn && urlInput) {
    pasteBtn.addEventListener('click', async () => {
        try {
            const text = await navigator.clipboard.readText();
            urlInput.value = text;
            urlInput.focus();
            pasteBtn.textContent = '✓ Pasted';
            pasteBtn.style.color = 'var(--success)';
            setTimeout(() => {
                pasteBtn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/></svg> Paste`;
                pasteBtn.style.color = '';
            }, 1500);
        } catch (e) {
            urlInput.focus();
        }
    });
}

// ===================== URL FORM VALIDATION =====================
const urlForm = document.getElementById('urlForm');

if (urlForm) {
    urlForm.addEventListener('submit', (e) => {
        const input = urlForm.querySelector('input[name="url"]');
        const val = input ? input.value.trim() : '';
        if (!val) {
            e.preventDefault();
            input && input.focus();
            return;
        }
        const btn = document.getElementById('submitBtn');
        if (btn) {
            btn.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="animation:spin 0.9s linear infinite"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Fetching...`;
            btn.disabled = true;
        }
    });
}

// ===================== INTERSECTION OBSERVER ANIMATIONS =====================
const observerOpts = {
    threshold: 0.1,
    rootMargin: '0px 0px -40px 0px'
};

const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting) {
            entry.target.style.animation = 'fadeInUp 0.5s ease both';
            observer.unobserve(entry.target);
        }
    });
}, observerOpts);

document.querySelectorAll('.feature-card, .step-card, .platform-card, .about-card, .sidebar-card').forEach(el => {
    el.style.opacity = '0';
    observer.observe(el);
});
