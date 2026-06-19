const { createApp, ref, reactive, onMounted, nextTick } = Vue;
const { darkTheme } = naive;

const DashboardApp = {
    template: '#dashboard-template',
    setup() {
        const message = window.naive.useMessage();

        // States
        const showDashboard = ref(false);
        const connecting = ref(false);
        const rememberKeys = ref(true);
        const statusText = ref('未连接');
        const totalEquityUsd = ref(0.0);
        const isSimulated = ref(false);
        
        const credentialsForm = reactive({
            api_key: '',
            secret_key: '',
            passphrase: '',
            environment: 'demo'
        });
        
        const envOptions = [
            { label: '模拟盘 (Demo Account)', value: 'demo' },
            { label: '实盘交易 (Live Account)', value: 'live' }
        ];
        
        const assets = ref([]);
        const positions = ref([]);
        const gridStrategies = ref([]);
        const priceHistory = ref({});
        
        // MA Bot States
        const maBotStatus = ref({
            is_running: false,
            is_paused: false,
            last_time: null,
            price: null,
            fast_ma: null,
            slow_ma: null,
            last_log: '',
            position: { side: 'flat', size: 0, avgPx: 0, upl: 0, uplRatio: 0 },
            balance: 0,
            last_signal: 'none'
        });
        const maBotLogs = ref([]);
        const maBotTrades = ref([]);
        const chartTimestamp = ref(Date.now());
        let lastChartFetchTime = Date.now();
        const refreshChart = () => {
            chartTimestamp.value = Date.now();
            lastChartFetchTime = Date.now();
        };
        
        // Modal states
        const showPasswordModal = ref(false);
        const passwordForm = reactive({
            old: '',
            new: '',
            confirm: '',
            error: '',
            success: '',
            submitting: false
        });
        
        let socket = null;
        let reconnectTimeout = null;
        let isExplicitDisconnect = false;
        
        // Path helper
        function getPathPrefix() {
            const path = window.location.pathname;
            if (path === '/') return '';
            if (path.includes('.')) {
                const segments = path.split('/');
                segments.pop();
                return segments.join('/');
            }
            if (path.endsWith('/')) {
                return path.slice(0, -1);
            }
            return path;
        }
        const prefix = getPathPrefix();
        const apiBase = window.location.protocol === 'file:' ? 'http://127.0.0.1:8000' : prefix;
        
        let maBotInterval = null;
        // Lifecycle hook
        onMounted(() => {
            checkServerConfig();
            fetchMaBotInfo();
            maBotInterval = setInterval(fetchMaBotInfo, 3000);
        });
        
        // Methods
        async function checkServerConfig() {
            try {
                const res = await fetch(apiBase + '/api/okx/config');
                if (!res.ok) throw new Error("Config fetch failed");
                
                const data = await res.json();
                if (data.has_keys) {
                    isSimulated.value = (data.environment !== 'live');
                    showDashboard.value = true;
                    connectPortfolio(true); // use server env keys
                } else {
                    loadStoredCredentials();
                }
            } catch (err) {
                console.error("Error checking server config:", err);
                loadStoredCredentials();
            }
        }
        
        function loadStoredCredentials() {
            const api_key = localStorage.getItem('okx_api_key');
            const secret_key = localStorage.getItem('okx_secret_key');
            const passphrase = localStorage.getItem('okx_passphrase');
            const environment = localStorage.getItem('okx_environment') || 'demo';
            
            if (api_key && secret_key && passphrase) {
                credentialsForm.api_key = api_key;
                credentialsForm.secret_key = secret_key;
                credentialsForm.passphrase = passphrase;
                credentialsForm.environment = environment;
                rememberKeys.value = true;
                
                isSimulated.value = (environment !== 'live');
                showDashboard.value = true;
                connectPortfolio(false); // use client keys
            }
        }
        
        function handleConnectForm() {
            const environment = credentialsForm.environment;
            isSimulated.value = (environment !== 'live');
            
            if (rememberKeys.value) {
                localStorage.setItem('okx_api_key', credentialsForm.api_key);
                localStorage.setItem('okx_secret_key', credentialsForm.secret_key);
                localStorage.setItem('okx_passphrase', credentialsForm.passphrase);
                localStorage.setItem('okx_environment', environment);
            } else {
                localStorage.removeItem('okx_api_key');
                localStorage.removeItem('okx_secret_key');
                localStorage.removeItem('okx_passphrase');
                localStorage.removeItem('okx_environment');
            }
            
            isExplicitDisconnect = false;
            showDashboard.value = true;
            connectPortfolio(false);
        }
        
        function connectPortfolio(useEnvKeys = false) {
            connecting.value = true;
            statusText.value = '正在建立 WebSocket 连接...';
            
            if (socket) {
                try { socket.close(); } catch (e) {}
            }
            if (reconnectTimeout) {
                clearTimeout(reconnectTimeout);
                reconnectTimeout = null;
            }
            
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = window.location.protocol === 'file:' 
                ? 'ws://127.0.0.1:8000/ws' 
                : `${protocol}//${window.location.host}${prefix}/ws`;
                
            console.log("Connecting to WebSocket:", wsUrl);
            socket = new WebSocket(wsUrl);
            
            socket.onopen = () => {
                connecting.value = false;
                statusText.value = useEnvKeys
                    ? (isSimulated.value ? '已加载本地 .env 配置 (模拟盘 - WS实时)' : '已加载本地 .env 配置 (实盘 - WS实时)')
                    : (isSimulated.value ? '模拟盘实时监控中 (WS)' : '实盘实时监控中 (WS)');
                
                const loginPayload = useEnvKeys 
                    ? { use_env: true }
                    : {
                        use_env: false,
                        api_key: credentialsForm.api_key,
                        secret_key: credentialsForm.secret_key,
                        passphrase: credentialsForm.passphrase,
                        simulated: isSimulated.value
                    };
                socket.send(JSON.stringify(loginPayload));
                message.success('WebSocket 连接建立成功，开始接收数据推送');
            };
            
            socket.onmessage = (e) => {
                try {
                    const data = JSON.parse(e.data);
                    if (data.success) {
                        totalEquityUsd.value = data.total_equity_usd;
                        assets.value = data.assets;
                        positions.value = data.positions;
                        gridStrategies.value = data.grid_strategies || [];
                        priceHistory.value = data.price_history || {};
                        
                        // Render sparkline charts on next tick when DOM canvas is available
                        nextTick(() => {
                            drawAllSparklines();
                        });
                    } else {
                        message.error("长连接验证出错: " + data.message);
                        handleDisconnect();
                    }
                } catch (err) {
                    console.error("Error parsing WebSocket message:", err);
                }
            };
            
            socket.onclose = (e) => {
                connecting.value = false;
                if (!isExplicitDisconnect) {
                    statusText.value = '数据推送断开，正在尝试重连中...';
                    reconnectTimeout = setTimeout(() => connectPortfolio(useEnvKeys), 5000);
                }
            };
            
            socket.onerror = (err) => {
                console.error("WebSocket error:", err);
            };
        }
        
        async function handleDisconnect() {
            try {
                const res = await fetch(apiBase + '/api/okx/config');
                const data = await res.json();
                if (data.has_keys) {
                    message.warning("当前配置由本地项目根目录的 .env 文件提供。若需关闭连接，请清空或删除服务器上的密钥文件，并重启服务器。");
                    return;
                }
            } catch (e) {}
            
            if (confirm("是否确定断开连接？断开后将清除本地密钥。")) {
                isExplicitDisconnect = true;
                if (socket) {
                    socket.close();
                    socket = null;
                }
                if (reconnectTimeout) {
                    clearTimeout(reconnectTimeout);
                    reconnectTimeout = null;
                }
                
                localStorage.removeItem('okx_api_key');
                localStorage.removeItem('okx_secret_key');
                localStorage.removeItem('okx_passphrase');
                localStorage.removeItem('okx_environment');
                
                credentialsForm.api_key = '';
                credentialsForm.secret_key = '';
                credentialsForm.passphrase = '';
                credentialsForm.environment = 'demo';
                
                showDashboard.value = false;
                message.info('连接已断开，本地密钥清除成功');
            }
        }
        
        function forceReconnect() {
            if (socket) {
                console.log("Forcing WebSocket reconnection...");
                socket.close();
                message.info('正在刷新实时数据...');
            } else {
                checkServerConfig();
            }
        }
        
        async function handleLogout() {
            if (confirm("是否确定退出登录？")) {
                try {
                    await fetch(prefix + '/api/logout', { method: 'POST' });
                    message.success('已安全登出系统');
                    setTimeout(() => {
                        window.location.reload();
                    }, 500);
                } catch (e) {
                    console.error("Logout failed:", e);
                }
            }
        }
        
        // Password Modal Methods
        function openPasswordModal() {
            passwordForm.old = '';
            passwordForm.new = '';
            passwordForm.confirm = '';
            passwordForm.error = '';
            passwordForm.success = '';
            passwordForm.submitting = false;
            showPasswordModal.value = true;
        }
        
        function closePasswordModal() {
            showPasswordModal.value = false;
        }
        
        async function handleChangePassword() {
            passwordForm.error = '';
            passwordForm.success = '';
            
            if (passwordForm.new.length < 6) {
                passwordForm.error = '新密码长度至少需要 6 位！';
                return;
            }
            if (passwordForm.new !== passwordForm.confirm) {
                passwordForm.error = '两次输入的新密码不一致！';
                return;
            }
            
            passwordForm.submitting = true;
            try {
                const res = await fetch(prefix + '/api/change-password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ old_password: passwordForm.old, new_password: passwordForm.new })
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    passwordForm.success = '密码修改成功，正在自动重新加载页面...';
                    message.success('密码修改成功，正在自动重新加载页面...');
                    setTimeout(() => {
                        window.location.reload();
                    }, 1500);
                } else {
                    passwordForm.error = data.message || '密码修改失败，请重试';
                    message.error(passwordForm.error);
                }
            } catch (err) {
                passwordForm.error = '网络请求失败，请稍后重试';
                message.error(passwordForm.error);
                console.error("Change password error:", err);
            } finally {
                passwordForm.submitting = false;
            }
        }
        
        async function fetchMaBotInfo() {
            try {
                const [statusRes, logsRes, tradesRes] = await Promise.all([
                    fetch(apiBase + '/api/ma-bot/status'),
                    fetch(apiBase + '/api/ma-bot/logs'),
                    fetch(apiBase + '/api/ma-bot/trades')
                ]);
                if (statusRes.ok) {
                    maBotStatus.value = await statusRes.json();
                }
                if (logsRes.ok) {
                    const logsData = await logsRes.json();
                    maBotLogs.value = logsData.logs;
                }
                if (tradesRes.ok) {
                    const tradesData = await tradesRes.json();
                    maBotTrades.value = (tradesData.trades || []).reverse();
                }
                
                // Refresh chart every 30 seconds automatically
                const now = Date.now();
                if (now - lastChartFetchTime > 30000) {
                    chartTimestamp.value = now;
                    lastChartFetchTime = now;
                }
            } catch (err) {
                console.error("Error fetching MA bot info:", err);
            }
        }

        async function toggleMaBotPause() {
            try {
                const res = await fetch(apiBase + '/api/ma-bot/toggle', {
                    method: 'POST'
                });
                if (res.ok) {
                    const data = await res.json();
                    if (data.success) {
                        maBotStatus.value.is_paused = data.is_paused;
                        message.success(data.is_paused ? '策略已暂停' : '策略已恢复运行');
                    } else {
                        message.error('操作失败: ' + data.error);
                    }
                } else {
                    message.error('网络请求失败，状态码: ' + res.status);
                }
            } catch (err) {
                console.error("Error toggling MA bot pause state:", err);
                message.error('网络请求失败');
            }
        }
        
        // Formatting Helpers
        function formatUSD(val) {
            if (val === undefined || val === null || isNaN(val)) return '--';
            return val.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
        }
        
        function formatNumber(val) {
            if (val === undefined || val === null || isNaN(val)) return '--';
            return val.toLocaleString();
        }
        
        function formatMaxDecimals(val, maxDecimals = 8) {
            if (val === undefined || val === null || isNaN(val)) return '--';
            return val.toLocaleString(undefined, {maximumFractionDigits: maxDecimals});
        }
        
        function formatDate(cTime) {
            if (!cTime) return '--';
            try {
                const date = new Date(parseInt(cTime));
                return date.toLocaleString();
            } catch(e) {
                return '--';
            }
        }
        
        // Sparklines drawing methods
        function drawAllSparklines() {
            // Draw sparklines for positions
            positions.value.forEach((p, idx) => {
                const canvas = document.getElementById(`sparkline-pos-${idx}`);
                const history = priceHistory.value[p.instId];
                if (canvas && history && history.length >= 2) {
                    drawSparkline(canvas, history);
                }
            });
            
            // Draw sparklines for grid strategies
            gridStrategies.value.forEach((g, idx) => {
                const canvas = document.getElementById(`sparkline-grid-${idx}`);
                const history = priceHistory.value[g.instId];
                if (canvas && history && history.length >= 2) {
                    drawSparkline(canvas, history);
                }
            });
        }
        
        function drawSparkline(canvas, prices) {
            if (!canvas || !prices || prices.length < 2) return;
            
            const ctx = canvas.getContext('2d');
            const width = canvas.width;
            const height = canvas.height;
            
            ctx.clearRect(0, 0, width, height);
            
            let min = Math.min(...prices);
            let max = Math.max(...prices);
            
            if (min === max) {
                min = min * 0.999;
                max = max * 1.001;
            }
            
            const range = max - min;
            
            const firstPrice = prices[0];
            const lastPrice = prices[prices.length - 1];
            const isUp = lastPrice >= firstPrice;
            
            const strokeColor = isUp ? 'rgb(16, 185, 129)' : 'rgb(239, 68, 68)';
            const fillColorStart = isUp ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.15)';
            const fillColorEnd = isUp ? 'rgba(16, 185, 129, 0.0)' : 'rgba(239, 68, 68, 0.0)';
            
            ctx.beginPath();
            
            const points = prices.map((price, idx) => {
                const x = (idx / (prices.length - 1)) * (width - 4) + 2;
                const y = height - ((price - min) / range) * (height - 6) - 3;
                return { x, y };
            });
            
            ctx.moveTo(points[0].x, points[0].y);
            for (let i = 1; i < points.length; i++) {
                ctx.lineTo(points[i].x, points[i].y);
            }
            
            ctx.strokeStyle = strokeColor;
            ctx.lineWidth = 1.5;
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            ctx.stroke();
            
            ctx.lineTo(points[points.length - 1].x, height);
            ctx.lineTo(points[0].x, height);
            ctx.closePath();
            
            const gradient = ctx.createLinearGradient(0, 0, 0, height);
            gradient.addColorStop(0, fillColorStart);
            gradient.addColorStop(1, fillColorEnd);
            ctx.fillStyle = gradient;
            ctx.fill();
            
            ctx.beginPath();
            ctx.arc(points[points.length - 1].x, points[points.length - 1].y, 2, 0, 2 * Math.PI);
            ctx.fillStyle = strokeColor;
            ctx.fill();
        }
        
        function getPositionProgress(pos) {
            if (!pos || pos.side === 'flat' || !pos.stop_loss || !pos.take_profit) return 50;
            const current = maBotStatus.value.price;
            if (!current) return 50;
            const sl = pos.stop_loss;
            const tp = pos.take_profit;
            if (pos.side === 'long') {
                if (tp === sl) return 50;
                const pct = ((current - sl) / (tp - sl)) * 100;
                return Math.min(Math.max(pct, 0), 100);
            } else {
                if (sl === tp) return 50;
                const pct = ((sl - current) / (sl - tp)) * 100;
                return Math.min(Math.max(pct, 0), 100);
            }
        }
        
        function getPositionEntryProgress(pos) {
            if (!pos || pos.side === 'flat' || !pos.stop_loss || !pos.take_profit) return 50;
            const entry = pos.avgPx;
            if (!entry) return 50;
            const sl = pos.stop_loss;
            const tp = pos.take_profit;
            if (pos.side === 'long') {
                if (tp === sl) return 50;
                const pct = ((entry - sl) / (tp - sl)) * 100;
                return Math.min(Math.max(pct, 0), 100);
            } else {
                if (sl === tp) return 50;
                const pct = ((sl - entry) / (sl - tp)) * 100;
                return Math.min(Math.max(pct, 0), 100);
            }
        }
        
        return {
            getPositionProgress,
            getPositionEntryProgress,
            showDashboard,
            connecting,
            rememberKeys,
            statusText,
            totalEquityUsd,
            isSimulated,
            credentialsForm,
            envOptions,
            assets,
            positions,
            gridStrategies,
            priceHistory,
            showPasswordModal,
            passwordForm,
            handleConnectForm,
            handleDisconnect,
            forceReconnect,
            handleLogout,
            openPasswordModal,
            closePasswordModal,
            handleChangePassword,
            formatUSD,
            formatNumber,
            formatMaxDecimals,
            formatDate,
            maBotStatus,
            maBotLogs,
            maBotTrades,
            chartTimestamp,
            refreshChart,
            apiBase,
            fetchMaBotInfo,
            toggleMaBotPause
        };
    }
};

const app = createApp({
    components: {
        DashboardApp
    },
    setup() {
        return {
            darkTheme
        };
    }
});
app.use(naive);
app.mount('#app');
