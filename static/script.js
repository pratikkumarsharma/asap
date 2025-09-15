document.addEventListener('DOMContentLoaded', () => {
    let currentUser = null;
    let qrUpdateInterval = null;
    let currentQRSession = null;
    let html5QrCode = null;
    let nativeStream = null;
    let fallbackAnimationFrame = null;
    let isScanning = false;
    const API_BASE = `${window.location.origin}/api`;

    const apiCall = async (endpoint, options = {}) => {
        const fetchOptions = {
            credentials: 'include',
            headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
            method: options.method || 'GET',
        };
        if (options.body) fetchOptions.body = JSON.stringify(options.body);

        try {
            const res = await fetch(`${API_BASE}${endpoint}`, fetchOptions);
            const data = await res.json();
            if (!res.ok) {
                const errMsg = data.message || data.error || data.detail || 'API Error';
                throw new Error(errMsg);
            }
            return data;
        } catch (err) {
            console.error('API Error:', err);
            throw err;
        }
    };

    // -------- Login --------
    const handleLoginForm = () => {
        const form = document.getElementById('login-form');
        if (!form) return;

        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = form.querySelector('button[type="submit"]');
            const errorDiv = document.getElementById('login-error');
            errorDiv.style.display = 'none';
            btn.disabled = true;
            const originalText = btn.textContent;
            btn.textContent = 'Logging in...';

            try {
                const payload = {
                    user_id: form.user_id.value.trim(),
                    password: form.password.value.trim()
                };
                const response = await apiCall('/login', { method: 'POST', body: payload });

                if (response.success && response.redirect) {
                    window.location.href = response.redirect;
                } else {
                    throw new Error(response.message || 'Invalid credentials');
                }
            } catch (err) {
                errorDiv.textContent = `Login failed: ${err.message}`;
                errorDiv.style.display = 'block';
            } finally {
                btn.disabled = false;
                btn.textContent = originalText;
            }
        });
    };

    // -------- Check Auth & Logout --------
    const checkAuthStatus = async () => {
        try {
            const data = await apiCall('/user');
            currentUser = data;
            await updateUIForUser();
        } catch {
            currentUser = null;
            if (!document.getElementById('login-form')) window.location.href = '/login';
        }
    };

    const logout = async () => {
        try {
            await apiCall('/logout', { method: 'POST' });
        } catch (e) {}
        currentUser = null;
        clearQRSession();
        stopAllScanners();
        window.location.href = '/login';
    };

    const stopAllScanners = () => {
        if (html5QrCode && isScanning) {
            html5QrCode.stop().catch(() => {}).finally(() => { html5QrCode = null; isScanning = false; });
        }
        if (nativeStream) {
            nativeStream.getTracks().forEach(track => track.stop());
            nativeStream = null;
        }
        if (fallbackAnimationFrame) {
            cancelAnimationFrame(fallbackAnimationFrame);
            fallbackAnimationFrame = null;
        }
    };

    // -------- Dashboard UI --------
    const updateUIForUser = async () => {
        const userInfo = document.getElementById('headerUserInfo');
        const avatar = document.getElementById('userAvatar');
        if (!userInfo || !avatar) return;

        if (!currentUser) {
            userInfo.style.display = 'none';
            avatar.title = 'Click to login';
            avatar.onclick = () => window.location.href = '/login';
            return;
        }

        userInfo.innerHTML = `
            <div style="font-weight:600">${(currentUser.role || '').charAt(0).toUpperCase() + (currentUser.role || '').slice(1)}</div>
            <div style="opacity:0.9">ID: ${currentUser.user_id}</div>`;
        userInfo.style.display = 'block';
        avatar.title = 'Click to logout';
        avatar.onclick = logout;

        if (currentUser.role === 'teacher') {
            renderTeacherControls();
        } else {
            const historyCard = document.getElementById('student-attendance-history');
            if (historyCard) historyCard.style.display = 'block';
            renderStudentControls();
            await updateAttendanceStats();
            await populateAttendanceTable();
        }
    };

    // -------- Teacher QR & Analytics --------
    const renderTeacherControls = () => {
        const cardBody = document.getElementById('attendance-card-body');
        if (!cardBody) return;

        cardBody.innerHTML = `
            <h3>QR Code Generator</h3>
            <div class="teacher-qr-controls">
                <div class="qr-settings" aria-hidden="false">
                    <select id="class-select" aria-label="Select class"></select>
                    <select id="duration-select" aria-label="Session duration">
                      <option value="15">15 min</option>
                      <option value="60" selected>1 hr</option>
                    </select>
                </div>
                <div class="qr-actions" style="margin-top:1rem;">
                    <button id="generate-qr-btn" class="btn btn-primary">Generate QR Code</button>
                    <button id="stop-qr-btn" class="btn btn-danger" style="display:none">Stop Session</button>
                </div>
                <div id="qr-display-container" style="display:none; margin-top:1rem;">
                    <div id="qr-display" class="qr-display"></div>
                    <p id="qr-expires-info" class="qr-token-info" aria-live="polite"></p>
                </div>
            </div>
        `;

        document.getElementById('generate-qr-btn').addEventListener('click', generateQRSession);
        document.getElementById('stop-qr-btn').addEventListener('click', stopQRSession);
        fetchAndPopulateClasses();
    };

    const fetchAndPopulateClasses = async () => {
        try {
            const resp = await apiCall('/classes');
            const select = document.getElementById('class-select');
            if (!select || !resp || !resp.classes) return;
            select.innerHTML = resp.classes.map(c => `<option value="${c.class_code}">${c.class_name}</option>`).join('');
        } catch (err) { console.error(err); }
    };

    const generateQRSession = async () => {
        try {
            const classCode = document.getElementById('class-select').value;
            const duration = document.getElementById('duration-select').value;
            const session = await apiCall('/teacher/generate-qr', {
                method: 'POST',
                body: { class_code: classCode, duration }
            });
            currentQRSession = { session_id: session.session_id, class_name: session.class_name, expires_at: new Date(session.expires_at) };
            startQRDisplay();
            const genBtn = document.getElementById('generate-qr-btn');
            const stopBtn = document.getElementById('stop-qr-btn');
            if (genBtn) genBtn.style.display = 'none';
            if (stopBtn) stopBtn.style.display = 'inline-flex';
            const container = document.getElementById('qr-display-container');
            if (container) container.style.display = 'block';
        } catch (err) {
            alert(err.message || 'Failed to create QR session');
        }
    };

    const startQRDisplay = () => {
        if (qrUpdateInterval) clearInterval(qrUpdateInterval);
        qrUpdateInterval = setInterval(updateQRCode, 2000);
        updateQRCode();
    };

    const updateQRCode = async () => {
        if (!currentQRSession) return;
        try {
            const tokenData = await apiCall('/qr-token');
            if (!tokenData || !tokenData.token) {
                clearQRSession();
                return;
            }
            const qrDisplay = document.getElementById('qr-display');
            const qrData = JSON.stringify({ token: tokenData.token, session_id: tokenData.session_id, timestamp: Date.now() });
            if (qrDisplay) {
                qrDisplay.innerHTML = '';
                try { new QRCode(qrDisplay, { text: qrData, width: 200, height: 200 }); }
                catch (e) { qrDisplay.textContent = tokenData.token; }
            }
            if (tokenData.expires_at && !currentQRSession.expires_at) {
                currentQRSession.expires_at = new Date(tokenData.expires_at);
            }
            updateQRInfo();
        } catch (err) {
            clearQRSession();
        }
    };

    const updateQRInfo = () => {
        if (!currentQRSession) return;
        const expiresInfo = document.getElementById('qr-expires-info');
        const now = new Date();
        const expiresAt = currentQRSession.expires_at ? new Date(currentQRSession.expires_at) : null;
        const secondsLeft = expiresAt ? Math.max(0, Math.floor((expiresAt - now) / 1000)) : 0;
        const minutes = Math.floor(secondsLeft / 60);
        const sec = String(secondsLeft % 60).padStart(2, '0');
        if (expiresInfo) {
            expiresInfo.textContent = secondsLeft > 0 ? `Expires in ${minutes}:${sec}` : 'Session expired.';
        }
        if (secondsLeft <= 0) clearQRSession();
    };

    const stopQRSession = async () => {
        try { await apiCall('/teacher/stop-qr', { method: 'POST' }); } catch {}
        clearQRSession();
    };

    const clearQRSession = () => {
        if (qrUpdateInterval) { clearInterval(qrUpdateInterval); qrUpdateInterval = null; }
        currentQRSession = null;
        const qrContainer = document.getElementById('qr-display-container');
        if (qrContainer) qrContainer.style.display = 'none';
        const genBtn = document.getElementById('generate-qr-btn');
        const stopBtn = document.getElementById('stop-qr-btn');
        if (genBtn) genBtn.style.display = 'inline-flex';
        if (stopBtn) stopBtn.style.display = 'none';
    };

    // -------- Student Controls with native fallback --------
    const renderStudentControls = () => {
        const cardBody = document.getElementById('attendance-card-body');
        if (!cardBody) return;

        cardBody.innerHTML = `
            <p>Scan QR Code</p>
            <button id="start-scan-btn" class="btn btn-primary">Start Scan</button>
            <div id="qr-reader-container" style="display:none; margin-top:1rem;">
                <div id="qr-reader"></div>
                <div id="qr-scan-result" aria-live="polite" style="margin-top:0.5rem;"></div>
            </div>
        `;
        document.getElementById('start-scan-btn').addEventListener('click', startQRScanner);
    };

    const loadScript = (src, timeout = 10000) => new Promise((resolve, reject) => {
        if (!src) return reject(new Error('No src provided'));
        const basename = src.split('/').pop();
        if (document.querySelector(`script[src="${src}"]`) || document.querySelector(`script[src$="/${basename}"]`)) {
            return setTimeout(() => resolve(), 50);
        }
        const s = document.createElement('script');
        s.src = src;
        s.async = true;
        let done = false;
        const timer = setTimeout(() => { if (!done) { done = true; s.onerror = s.onload = null; reject(new Error('Script load timeout')); } }, timeout);
        s.onload = () => { if (done) return; done = true; clearTimeout(timer); resolve(); };
        s.onerror = (e) => { if (done) return; done = true; clearTimeout(timer); reject(new Error('Failed to load script: ' + src)); };
        document.head.appendChild(s);
    });

    const ensureHtml5QrCodeLoaded = async () => {
        if (typeof Html5Qrcode !== 'undefined' && !Html5Qrcode.__isBootstrapStub) return;
        const localRelative = `/static/html5-qrcode.min.js`;
        const localOrigin = `${window.location.origin}/static/html5-qrcode.min.js`;
        const cdnUnpkg = 'https://unpkg.com/html5-qrcode@2.3.8/minified/html5-qrcode.min.js';
        const cdnJsDelivr = 'https://cdn.jsdelivr.net/npm/html5-qrcode@2.3.8/minified/html5-qrcode.min.js';
        const tries = [localRelative, localOrigin, cdnUnpkg, cdnJsDelivr];
        for (const src of tries) {
            try { await loadScript(src); await new Promise(r => setTimeout(r, 50)); if (typeof Html5Qrcode !== 'undefined' && !Html5Qrcode.__isBootstrapStub) return; }
            catch (e) { console.warn('Failed to load script', src, e); }
        }
        throw new Error('Camera scanning library (Html5Qrcode) could not be loaded.');
    };

    const startQRScanner = async () => {
        const container = document.getElementById('qr-reader-container');
        const startBtn = document.getElementById('start-scan-btn');
        if (container) container.style.display = 'block';
        if (startBtn) startBtn.style.display = 'none';

        const hostname = window.location.hostname;
        if (!window.isSecureContext && hostname !== 'localhost' && hostname !== '127.0.0.1') {
            alert('Camera access requires HTTPS or localhost.');
            resetQRScanner();
            return;
        }

        try {
            await ensureHtml5QrCodeLoaded();
            Html5Qrcode.getCameras().then(cameras => {
                if (!cameras || cameras.length === 0) throw new Error('No camera devices found.');

                let cameraId = cameras[0].id;
                const backCam = cameras.find(cam => /back|rear|environment/i.test(cam.label));
                if (backCam) cameraId = backCam.id;

                html5QrCode = new Html5Qrcode('qr-reader');
                isScanning = true;

                html5QrCode.start(
                    cameraId,
                    { fps: 10, qrbox: 250 },
                    decodedText => { isScanning = false; html5QrCode.stop().finally(() => { html5QrCode = null; processScannedData(decodedText); }); },
                    errorMessage => console.warn("QR scan error:", errorMessage)
                ).catch(err => { console.warn("Html5Qrcode start failed, falling back:", err); startNativeFallback(); });

            }).catch(err => { console.warn("Camera enumeration failed, falling back:", err); startNativeFallback(); });
        } catch (err) { console.warn("Html5Qrcode load failed, using fallback:", err); startNativeFallback(); }
    };

    const startNativeFallback = async () => {
        if (!('mediaDevices' in navigator)) { alert('Camera not supported.'); resetQRScanner(); return; }
        const videoEl = document.createElement('video');
        videoEl.setAttribute('autoplay', true);
        videoEl.setAttribute('playsinline', true);
        document.getElementById('qr-reader').innerHTML = '';
        document.getElementById('qr-reader').appendChild(videoEl);

        try {
            nativeStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
            videoEl.srcObject = nativeStream;
            await videoEl.play();

            if (!('BarcodeDetector' in window)) {
                alert('BarcodeDetector API not supported.');
                resetQRScanner();
                return;
            }

            const barcodeDetector = new BarcodeDetector({ formats: ['qr_code'] });

            const scanFrame = async () => {
                if (!videoEl || videoEl.readyState !== 4) { fallbackAnimationFrame = requestAnimationFrame(scanFrame); return; }
                try {
                    const barcodes = await barcodeDetector.detect(videoEl);
                    if (barcodes.length > 0) { processScannedData(barcodes[0].rawValue); resetQRScanner(); return; }
                } catch (e) { console.warn("Barcode detection error:", e); }
                fallbackAnimationFrame = requestAnimationFrame(scanFrame);
            };
            scanFrame();

        } catch (err) { alert('Cannot access camera: ' + err.message); resetQRScanner(); }
    };

    const processScannedData = async (qrData) => {
        const resultEl = document.getElementById('qr-scan-result');
        if (resultEl) resultEl.textContent = 'Processing...';
        try {
            const parsed = JSON.parse(qrData);
            const resp = await apiCall('/student/scan-qr', { method: 'POST', body: parsed });
            if (resultEl) resultEl.textContent = `✅ ${resp.message}`;
            await updateAttendanceStats();
            await populateAttendanceTable();
        } catch (err) {
            if (resultEl) resultEl.textContent = `❌ ${err.message || 'Invalid QR or network error'}`;
        }
        setTimeout(resetQRScanner, 2500);
    };

    const resetQRScanner = () => {
        stopAllScanners();
        const container = document.getElementById('qr-reader-container');
        if (container) container.style.display = 'none';
        const startBtn = document.getElementById('start-scan-btn');
        if (startBtn) startBtn.style.display = 'inline-flex';
    };

    const updateAttendanceStats = async () => {
        if (!currentUser || currentUser.role !== 'student') return;
        try {
            const data = await apiCall('/student/attendance-history');
            const attendance_records = data.attendance_records || [];
            const present = attendance_records.filter(r => r.status === 'present').length;
            const percentage = attendance_records.length ? Math.round((present / attendance_records.length) * 100) : 0;
            const el = document.getElementById('overall-attendance');
            if (el) el.textContent = `${percentage}%`;
        } catch (err) { console.error(err); }
    };

    const populateAttendanceTable = async () => {
        if (!currentUser || currentUser.role !== 'student') return;
        try {
            const { attendance_records } = await apiCall('/student/attendance-history');
            const tbody = document.getElementById('attendance-tbody');
            if (!tbody) return;
            tbody.innerHTML = '';
            for (const rec of attendance_records) {
                const tr = document.createElement('tr');
                tr.innerHTML = `<td class="px-6 py-4 text-sm">${rec.date}</td><td class="px-6 py-4 text-sm">${rec.status}</td><td class="px-6 py-4 text-sm">${rec.class_name || '-'}</td>`;
                tbody.appendChild(tr);
            }
        } catch (err) { console.error(err); }
    };

    // -------- Navigation & Theming --------
    const setupNavigation = () => {
        document.querySelectorAll('.nav-item').forEach(n => n.addEventListener('click', e => showSection(e.currentTarget.dataset.section)));
        document.querySelectorAll('.feature-card[data-section-target]').forEach(f => f.addEventListener('click', e => showSection(e.currentTarget.dataset.sectionTarget)));
        const themeToggle = document.getElementById('themeToggle');
        if (themeToggle) themeToggle.addEventListener('click', () => {
            const dark = document.body.classList.toggle('dark-mode');
            document.getElementById('themeIcon').textContent = dark ? '🌞' : '🌙';
            themeToggle.setAttribute('aria-pressed', dark ? 'true' : 'false');
        });
        document.querySelectorAll('.nav-item, .emergency-btn').forEach(el => {
            el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); el.click(); } });
        });
    };

    const showSection = (id) => {
        document.querySelectorAll('.section.active, .nav-item.active').forEach(el => el.classList.remove('active'));
        const target = document.getElementById(id);
        if (target) target.classList.add('active');
        const nav = document.querySelector(`.nav-item[data-section="${id}"]`);
        if (nav) nav.classList.add('active');
    };

    // -------- Initialize --------
    const init = async () => {
        if (document.getElementById('login-form')) handleLoginForm();
        else {
            setupNavigation();
            showSection('dashboard');
            await checkAuthStatus();
        }
    };
    init();
});
