/* Gaia upload lifecycle only. Preparation stays in gaia-workspace.js. */
window.createGaiaUploadController = function ({onChange = () => {}} = {}) {
  const button = document.querySelector('#gaiaSubmitBtn');
  const status = document.querySelector('#gaiaUploadStatus');
  const form = document.querySelector('#gaiaFormFields');
  const advanced = document.querySelector('#gaiaAdvancedFields');
  let batch = null, enabled = false, busy = false, timer = null, disposed = false;
  let stopping = false, stopUncertain = false, startUncertain = false, revision = 0;
  const requests = new Set();
  // A missing HTTP response is not proof of cancellation or submission failure.
  // Time out the transport, then reconcile the SAME batch, never resend Start.
  async function request(url, options = {}, timeoutMs = 10000) {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), timeoutMs);
    requests.add(abort);
    try {
      const response = await fetch(url, {...options, signal: abort.signal});
      return {status: response.status, data: await response.json()};
    } finally {
      clearTimeout(timeout);
      requests.delete(abort);
    }
  }
  const submitted = () => batch?.gaia_receipt?.submit_intent
    || ['gaia_submitted', 'gaia_accepted', 'gaia_rejected', 'doudian_drafts_matched', 'handed_off'].includes(batch?.state);
  const active = value => ['confirmed', 'gaia_submitted'].includes(value?.state)
    && ['uploading', 'submitting'].includes(value?.gaia_receipt?.phase)
    || value?.gaia_receipt?.split_phase === 'running'
    || ['gaia_accepted', 'doudian_drafts_matched'].includes(value?.state)
      && ['waiting_gaia', 'ids_verified', 'submitting'].includes(value?.gaia_receipt?.price_phase)
    || value?.state === 'handed_off'
      && ['waiting_price', 'preparing', 'submitting'].includes(value?.gaia_receipt?.activity_phase)
    || value?.state === 'cancelled' && value?.gaia_receipt?.phase === 'stopping';
  const canStop = () => !disposed && !stopping && !stopUncertain
    && batch?.id && batch?.shop_id && batch?.output_hash
    && !submitted() && ['validation_passed', 'confirmed'].includes(batch.state);
  const canStart = () => enabled && batch?.state === 'validation_passed'
    && !busy && !disposed && !batch.gaia_receipt?.execution_key;
  const stopStatus = () => {
    if (stopping) return {kind: 'stopping', enabled: false, label: '正在停止…',
      hint: '已发送停止请求，正在等待服务确认；请勿重复启动。'};
    if (batch?.state === 'cancelled') {
      if (batch.gaia_receipt?.phase === 'stopping') return {kind: 'stopping', enabled: false,
        label: '正在停止…', hint: '已阻止提交，正在安全退出浏览器；当前操作返回或超时后关闭。'};
      return {kind: 'stopped', enabled: false, label: '已停止', hint: '已停止，未提交盖亚；已生成的文件和记录保留。'};
    }
    if (submitted()) return {kind: 'submitted', enabled: false, label: '已提交，无法撤回',
      hint: '已进入盖亚提交阶段，本地停止不能撤回；请查看本批结果，不要重复提交。'};
    if (stopUncertain) return {kind: 'uncertain', enabled: false, label: '停止待确认',
      hint: '尚未确认是否停止，正在重新查询原批次；请勿重复启动。'};
    if (canStop()) return {kind: 'available', enabled: true, label: '停止任务',
      hint: busy ? '提交前可停止。点击后阻止提交，并在安全点退出浏览器。' : '文件尚未提交，可停止本批次。'};
    if (busy) return {kind: 'checking', enabled: false, label: '正在核对状态…',
      hint: '正在读取原批次状态，请勿重复启动。'};
    return {kind: 'idle', enabled: false, label: batch ? '任务已结束' : '停止任务',
      hint: batch ? '当前没有可停止的执行，请查看本批结果。' : '任务开始后可在提交前停止；已提交盖亚的任务无法撤回。'};
  };
  const render = () => {
    if (advanced) advanced.disabled = form.disabled;
    button.disabled = !canStart();
    button.textContent = busy ? '盖亚处理中…' : '上传已生成文件';
    onChange({batch, busy, canStart: Boolean(canStart()), canStop: Boolean(canStop()), stopStatus: stopStatus()});
  };
  const remember = () => sessionStorage.setItem('gaia-upload-batch', batch.id);
  async function poll() {
    if (!batch || disposed || stopping) return;
    const version = ++revision;
    clearTimeout(timer);
    try {
      const response = await request('/api/gaia/batches/' + encodeURIComponent(batch.id));
      const data = response.data;
      if (disposed || version !== revision || stopping) return;
      if (response.status === 404) {
        if (stopUncertain || startUncertain) throw new Error('执行结果待确认');
        batch = null;
        busy = false;
        form.disabled = false;
        sessionStorage.removeItem('gaia-upload-batch');
        status.textContent = '上次批次记录不存在，请重新生成文件。';
        render();
        return;
      }
      if (!data.success) throw new Error(data.message || '读取状态失败');
      batch = data.batch;
      stopUncertain = false;
      if (batch.state !== 'validation_passed' || batch.gaia_receipt?.execution_key) startUncertain = false;
      busy = startUncertain || active(batch);
      form.disabled = busy;
      if (startUncertain) status.textContent = '启动回执待确认，正在查询原批次；可停止本批次，请勿重复启动。';
      else if (batch.gaia_receipt?.message) status.textContent = batch.gaia_receipt.message;
      render();
      if (busy) timer = setTimeout(poll, 1500);
    } catch (error) {
      if (disposed || version !== revision || stopping) return;
      // Losing the response does not mean the submission failed. Keep polling
      // the original batch instead of creating or resending a task.
      status.textContent = stopUncertain ? stopStatus().hint
        : '暂时无法读取盖亚进度，正在重连；请勿重复提交。';
      render();
      timer = setTimeout(poll, 3000);
    }
  }
  async function start() {
    if (!canStart()) return {success: false, message: '本批次不可重复启动，请查看执行状态'};
    busy = true;
    const version = ++revision;
    form.disabled = true;
    remember();
    render();
    status.textContent = '正在启动盖亚专用浏览器…';
    try {
      const response = await request('/api/gaia/batches/' + encodeURIComponent(batch.id) + '/start', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({shop_id: batch.shop_id, expected_output_hash: batch.output_hash,
          confirmed: true,
          activity_name: document.querySelector('#gaiaActivityName')?.value?.trim() || '',
          installment_terms: Array.from(document.querySelector('#gaiaPaymentOptions')
            ?.querySelectorAll('input[type="checkbox"]:checked') || [], input => input.value)
            .filter(value => value !== 'full').map(Number).sort((a, b) => a - b)})
      }, 60000);
      const data = response.data;
      if (stopping || version !== revision || disposed) return {success: true};
      if (!data.success) {
        busy = false;
        form.disabled = false;
        status.textContent = data.message || '盖亚启动被阻断';
        render();
        return {success: false, message: status.textContent};
      }
      batch = data.batch;
      await poll();
    } catch (error) {
      if (disposed || version !== revision || stopping) return {success: true};
      startUncertain = true;
      status.textContent = '启动响应中断，正在查询原批次；不会重复提交。';
      await poll();
    }
    return {success: true};
  }
  async function stop() {
    if (!canStop()) return {success: false, message: '当前批次无法停止；已提交到盖亚的任务不能撤回'};
    stopping = true;
    ++revision; // Discard a GET issued before stop; it must not resurrect uploading.
    clearTimeout(timer);
    busy = true;
    form.disabled = true;
    remember();
    render();
    status.textContent = '正在停止，请稍候…';
    let result;
    try {
      const response = await request('/api/gaia/batches/' + encodeURIComponent(batch.id) + '/stop', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({shop_id: batch.shop_id, expected_output_hash: batch.output_hash})
      });
      result = response.data;
      if (disposed) return result;
      if (result.success) batch = result.batch;
      else {
        result.message = result.message || '停止未确认，请核对任务状态';
        status.textContent = result.message;
      }
    } catch (error) {
      result = {success: false, message: '停止响应中断，正在查询原批次；请勿重复启动'};
      if (!disposed) {
        stopUncertain = true;
        status.textContent = result.message;
      }
    } finally {
      stopping = false;
    }
    if (disposed) return result;
    // The POST receipt is authoritative even if the following read fails.
    busy = result.success ? active(batch) : true;
    form.disabled = busy;
    render();
    await poll();
    if (disposed) return result;
    if (batch?.state === 'cancelled') result = {success: true, batch};
    if (!result.success && !stopUncertain) status.textContent = result.message;
    return result;
  }
  button.addEventListener('click', start);
  window.addEventListener('pagehide', () => {
    disposed = true;
    ++revision;
    clearTimeout(timer);
    for (const abort of requests) abort.abort();
  }, {once: true});
  const previous = sessionStorage.getItem('gaia-upload-batch');
  if (previous && /^[a-f0-9]+$/.test(previous)) {
    batch = {id: previous};
    busy = true;
    form.disabled = true;
    poll();
  }
  return {
    isBusy: () => busy,
    canStart: () => Boolean(canStart()),
    canStop: () => Boolean(canStop()),
    stopStatus,
    stop,
    start,
    refresh: poll,
    setEnabled(value) { enabled = value; render(); },
    setBatch(value) {
      if (busy) return;
      batch = value;
      stopUncertain = false;
      startUncertain = false;
      ++revision;
      clearTimeout(timer);
      sessionStorage.removeItem('gaia-upload-batch');
      status.textContent = value
        ? '已填好 ' + (value.summary.split_total_rows || value.summary.generated_rows)
          + ' 行' + (value.summary.split_part_total > 1 ? '，分成 ' + value.summary.split_part_total + ' 份' : '')
          + '，等待上传。'
        : '填写店铺、成色和型号后点击“开始执行”，自动匹配、填表并上传盖亚。';
      render();
    }
  };
};
