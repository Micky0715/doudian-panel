/* Gaia workspace orchestration only; never starts Doudian tasks. */
window.initGaiaWorkspace = function ({$, toast}) {
  const choose = $('#gaiaChooseFile');
  if (!choose) return;
  const templatePicker = $('#gaiaTemplateFile');
  const chooseTemplate = $('#gaiaChooseTemplateFile');
  const mappingPicker = $('#gaiaMappingFile');
  const chooseMapping = $('#gaiaChooseMappingFile');
  const loadDownloadedMapping = $('#gaiaLoadDownloadedMapping');
  const uploadMapping = $('#gaiaUploadMappingBtn');
  const condition = $('#gaiaCondition');
  const previewBtn = $('#gaiaPreviewBtn');
  const generateBtn = $('#gaiaParseBtn');
  const startBtn = $('#gaiaStartBtn');
  const stopBtn = $('#gaiaStopBtn');
  const resolveModelsBtn = $('#gaiaResolveModelsBtn');
  const handoffBtn = $('#gaiaHandoffCheckBtn');
  const cancelBtn = $('#gaiaCancelBatchBtn');
  const downloadLink = $('#gaiaDownloadFile');
  $('#gaiaDiagnoseResultBtn')?.addEventListener('click', async () => {
    const target = $('#gaiaResultDiagnosis');
    const file = $('#gaiaResultFile')?.files?.[0];
    if (!generatedBatchId || !file) {
      target.textContent = '请先选择本批盖亚结果表；当前页面没有批次时，请勿把结果关联到其他批次。';
      return;
    }
    const form = new FormData();
    form.append('shop_id', $('#gaiaShop')?.value || '');
    form.append('file', file);
    target.textContent = '正在只读核对结果表…';
    const response = await fetch('/api/gaia/batches/' + generatedBatchId + '/result-diagnosis', {
      method: 'POST', body: form
    }).then(r => r.json()).catch(() => ({success: false, message: '结果表读取失败'}));
    if (!response.success) {
      target.textContent = response.message || '结果表与当前批次无法核对';
      return;
    }
    const report = response.diagnosis;
    const roots = report.primary_errors.map(item => {
      const snapshot = item.goods_in_mapping_snapshot === false
        ? '；本批历史映射未收录此货品，请核对盖亚当前货品和规则价格' : '';
      return `第 ${item.excel_row} 行：货品 ${item.goods_id || '未知'}，标品 ${item.standard_sku_id}`
        + `，成色 ${item.quality_id}：${item.error}${snapshot}`;
    });
    const coverage = report.pair_coverage === 'partial'
      ? `结果表缺少本批 ${report.missing_sku_quality_pairs} 个标品成色组合，不能确认覆盖全部上传行。`
      : '';
    target.textContent = `结果表 ${report.result_rows} 行，首发错误 ${roots.length} 行，连带错误 `
      + `${report.cascade_error_count} 行。${roots.join('；') || '未记录明确首发错误'}。`
      + coverage + (report.next_step || '')
      + ' 结果表没有本地批次唯一标识，商品是否最终创建仍需平台回查。';
  });
  const priceTemplate = $('#gaiaPriceTemplate');
  const priceStatus = $('#gaiaPriceUpliftStatus');
  const priceResult = $('#gaiaPriceResult');
  const priceDownload = $('#gaiaPriceDownload');
  const priceResume = $('#gaiaPriceResume');
  const activityField = $('#gaiaActivityField');
  const activityName = $('#gaiaActivityName');
  const activityDraftKey = 'gaia-activity-name-by-shop-scene-terms';
  let activityDrafts = {};
  try {
    const stored = JSON.parse(sessionStorage.getItem(activityDraftKey) || '{}');
    if (stored && typeof stored === 'object' && !Array.isArray(stored)) activityDrafts = stored;
  } catch (_) {}
  let activityDraftId = '';
  function selectedPayment() {
    return Array.from($('#gaiaPaymentOptions')?.querySelectorAll('input[type="checkbox"]:checked') || [],
      input => input.value);
  }
  function selectedInstallmentTerms() {
    return selectedPayment().filter(value => value !== 'full').map(Number).sort((a, b) => a - b);
  }
  function syncActivityField() {
    const shopId = $('#gaiaShop')?.value || '';
    const terms = selectedInstallmentTerms();
    const applicable = terms.length > 0;
    const nextId = applicable && shopId ? [shopId, $('#gaiaScene')?.value, terms.join('+')].join(':') : '';
    if (activityField) activityField.hidden = !applicable;
    if (activityName) {
      activityName.disabled = !applicable;
      if (activityDraftId !== nextId) activityName.value = nextId ? String(activityDrafts[nextId] || '') : '';
    }
    activityDraftId = nextId;
  }
  activityName?.addEventListener('input', () => {
    if (!activityDraftId) return;
    const value = activityName.value.slice(0, 120);
    if (value.trim()) activityDrafts[activityDraftId] = value;
    else delete activityDrafts[activityDraftId];
    try { sessionStorage.setItem(activityDraftKey, JSON.stringify(activityDrafts)); } catch (_) {}
  });
  let preparing = false;
  let stopRequested = false;
  let preparationStopped = false;
  let mappingChosenManually = false;
  let gaiaSubmitReady = false;
  let mappingError = '';
  let selectedSourcePath = '';
  let templateRequestSeq = 0;
  let mappingVersionId = '';
  let templateVersionLabel = '模板待检查';
  let mappingVersionLabel = '映射表待检查';
  let previewKey = '';
  let previewMeta = null;
  let generatedBatchId = '';
  let qualityStatus = {};
  let gaiaBackendReady = false;
  let safeMainlandMappingReady = false;
  let matchedModelSignature = '';
  let pricePreparedKey = '';
  function selectedQualities() {
    return Array.from(condition?.querySelectorAll('input[type="checkbox"]:checked') || [], input => input.value);
  }
  function pricePayload() {
    return {shop_id: $('#gaiaShop')?.value || '',
      installments: selectedInstallmentTerms().at(-1) ?? null};
  }
  function showPriceError(message) {
    pricePreparedKey = '';
    priceDownload.disabled = true;
    priceStatus.textContent = '待核实';
    priceStatus.className = 'status-chip neutral';
    priceResult.textContent = message;
  }
  $('#gaiaPriceTemplateUpload')?.addEventListener('click', async () => {
    const file = priceTemplate?.files?.[0];
    if (!file) return showPriceError('请先选择盖亚价格上浮官方导入模板。');
    const form = new FormData();
    form.append('file', file);
    const response = await fetch('/api/gaia/price-uplift/template', {method: 'POST', body: form})
      .then(r => r.json()).catch(() => ({success: false, message: '模板上传失败'}));
    if (!response.success) return showPriceError(response.message);
    priceResult.textContent = response.message + '；下一步检查本批商品 ID。';
  });
  fetch('/api/gaia/price-uplift/template/current').then(r => r.json()).then(response => {
    if (response.success) {
      priceResult.textContent = '已找到有效官方导入模板。点击“开始执行”后会自动关联本批商品并导入所选期数。';
    } else {
      priceResult.textContent = response.message || '请先上传官方导入模板。';
    }
  }).catch(() => {});
  $('#gaiaPricePreview')?.addEventListener('click', async () => {
    if (pricePayload().installments === null) return showPriceError('已选择全款，无需检查或导入分期规则。');
    if (!generatedBatchId) return showPriceError('本次页面尚无盖亚批次，请先完成盖亚发品。');
    const payload = pricePayload();
    const response = await fetch('/api/gaia/batches/' + generatedBatchId + '/price-uplift/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    }).then(r => r.json()).catch(() => ({success: false, message: '价格上浮预检失败'}));
    if (!response.success) return showPriceError(response.message);
    pricePreparedKey = JSON.stringify({batch_id: generatedBatchId, ...payload});
    priceDownload.disabled = false;
    priceStatus.textContent = '可生成';
    priceStatus.className = 'status-chip ready';
    priceResult.textContent = '已核实 ' + response.preview.count + ' 个抖店商品 ID；目标店铺 '
      + payload.shop_id + '，规则“' + response.preview.rule_name + '”。“开始执行”会自动导入；也可下载核对。';
  });
  $('#gaiaPaymentOptions')?.addEventListener('change', event => {
    const changed = event?.target;
    const inputs = Array.from($('#gaiaPaymentOptions')?.querySelectorAll('input[type="checkbox"]') || []);
    if (changed?.checked && changed.value === 'full') {
      inputs.filter(input => input.value !== 'full').forEach(input => {input.checked = false;});
    } else if (changed?.checked) {
      const full = inputs.find(input => input.value === 'full');
      if (full) full.checked = false;
    }
    showPriceError(pricePayload().installments === null
      ? '已选择全款：照常执行盖亚发品，跳过分期规则导入。'
      : '分期数已变更，请重新检查本批商品 ID。');
    $('#gaiaPricePreview').disabled = pricePayload().installments === null;
    refreshGaiaReadiness();
  });
  priceDownload?.addEventListener('click', async () => {
    const payload = pricePayload();
    if (payload.installments === null) return showPriceError('全款无需生成分期导入表。');
    if (pricePreparedKey !== JSON.stringify({batch_id: generatedBatchId, ...payload})) {
      return showPriceError('店铺、期数或批次已变化，请重新检查。');
    }
    const response = await fetch('/api/gaia/batches/' + generatedBatchId + '/price-uplift/download', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      return showPriceError(error.message || '导入文件生成失败');
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = 'price_uplift_' + generatedBatchId + '_' + payload.installments + '.xlsx';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const uploadController = window.createGaiaUploadController({onChange: ({batch, busy, stopStatus}) => {
    if (batch?.gaia_receipt?.execution_key && !generatedBatchId) {
      generatedBatchId = batch.id;
      previewKey = JSON.stringify(generationPayload());
    }
    if (batch?.gaia_receipt?.execution_key || batch?.state === 'cancelled' || stopStatus.kind === 'uncertain') {
      const status = $('#gaiaGenerationStatus');
      const accepted = batch.state === 'gaia_accepted';
      status.textContent = stopStatus.kind === 'uncertain' ? '停止待确认'
        : batch.state === 'cancelled' ? (busy ? '正在停止' : '已停止')
        : busy ? '盖亚处理中' : accepted ? '盖亚商品已核实' : '请查看执行结果';
      status.className = 'status-chip ' + (busy || batch.state === 'cancelled' ? 'neutral' : accepted ? 'ready' : 'error');
    }
    const pricePhase = batch?.gaia_receipt?.price_phase;
    if (priceResume) {
      const receipt = batch?.gaia_receipt || {};
      const verified = receipt.result?.verified_product_ids;
      const resumeAttempts = Number(receipt.price_resume_attempts || (receipt.price_resume_started ? 1 : 0));
      const recoverable = batch?.state === 'manual_required' && receipt.phase === 'accepted'
        && pricePhase === 'failed' && receipt.submit_intent === true
        && !receipt.price_submit_intent && resumeAttempts < 2
        && Array.isArray(verified) && verified.length > 0
        && JSON.stringify(verified) === JSON.stringify(batch.doudian_draft_ids)
        && verified.length === batch.summary?.expected_product_count;
      priceResume.hidden = !recoverable;
      priceResume.disabled = !recoverable || busy;
    }
    if (pricePhase && pricePhase !== 'not_requested') {
      const labels = {waiting_gaia: '等待盖亚', ids_verified: '已核实商品',
        submitting: '导入中', accepted: '导入成功', id_blocked: '商品关联失败',
        rejected: '导入失败', unknown: '结果待核对', failed: '导入未完成'};
      priceStatus.textContent = labels[pricePhase] || pricePhase;
      priceStatus.className = 'status-chip ' + (pricePhase === 'accepted' ? 'ready'
        : ['id_blocked', 'rejected', 'unknown', 'failed'].includes(pricePhase) ? 'error' : 'neutral');
      priceResult.textContent = batch.gaia_receipt.message || priceResult.textContent;
    }
    const activityPhase = batch?.gaia_receipt?.activity_phase;
    if (activityPhase && activityPhase !== 'not_requested') {
      const labels = {waiting_price: '等待价格导入', preparing: '核对活动中', submitting: '活动提交中',
        verified: '报名已核实', submitted_unverified: '提交待核对',
        failed: '提交前停止', unknown: '结果待核对', blocked: '条件不符'};
      priceResult.textContent = (batch.gaia_receipt.message || '')
        + '；分期免息活动：' + (labels[activityPhase] || activityPhase);
    }
    refreshGaiaBackendControls();
  }});
  priceResume?.addEventListener('click', async () => {
    if (priceResume.disabled || !generatedBatchId) return;
    priceResume.disabled = true;
    priceResult.textContent = '正在恢复已创建商品的分期导入；不会重新上传盖亚发品表。';
    const batch = await fetch('/api/gaia/batches/' + generatedBatchId)
      .then(r => r.json()).then(v => v.batch).catch(() => null);
    if (!batch?.output_hash) {
      priceResult.textContent = '无法回读原批次，未启动分期恢复。';
      await uploadController.refresh();
      return;
    }
    const response = await fetch('/api/gaia/batches/' + generatedBatchId + '/price-uplift/resume', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({shop_id: batch.shop_id, expected_output_hash: batch.output_hash, confirmed: true})
    }).then(r => r.json()).catch(() => ({success: false, message: '恢复请求状态待核对'}));
    if (!response.success) priceResult.textContent = response.message || '分期恢复未启动';
    await uploadController.refresh();
  });
  if (loadDownloadedMapping) loadDownloadedMapping.disabled = true;
  if (uploadMapping) uploadMapping.disabled = true;

  function showInputVersions() {
    const output = $('#gaiaInputVersions');
    if (output) output.textContent = templateVersionLabel + '；' + mappingVersionLabel;
  }

  function versionTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN');
  }

  function refreshGaiaBackendControls() {
    const advanced = $('#gaiaAdvancedFields');
    if (advanced) advanced.disabled = preparing || uploadController.isBusy();
    if (stopBtn) {
      let view = uploadController.stopStatus();
      if (preparing && !uploadController.isBusy() && !['stopped', 'submitted'].includes(view.kind)) {
        view = stopRequested ? {enabled: false, label: '正在停止…',
          hint: '已阻止后续上传，等待当前读取或文件生成结束；不会继续启动浏览器。'}
          : {enabled: true, label: '停止任务', hint: '正在准备文件，可停止后续匹配、生成和上传。'};
        if (stopRequested) {
          $('#gaiaGenerationStatus').textContent = '正在停止';
          $('#gaiaUploadStatus').textContent = view.hint;
        }
      } else if (preparationStopped && !generatedBatchId && !preparing) {
        view = {enabled: false, label: '已停止', hint: '已停止，未上传或提交盖亚。'};
      }
      stopBtn.disabled = !view.enabled;
      stopBtn.textContent = view.label;
      stopBtn.title = view.hint;
      $('#gaiaStopHint').textContent = view.hint;
    }
    if (startBtn) {
      const busy = uploadController.isBusy();
      const processed = Boolean(generatedBatchId) && !uploadController.canStart();
      const missingQualityId = selectedQualities().some(label => qualityStatus[label]?.configured === false);
      startBtn.disabled = preparing || busy || processed || missingQualityId || !gaiaBackendReady || !gaiaSubmitReady;
      startBtn.textContent = preparing ? '正在处理…' : busy ? '盖亚处理中…'
        : processed ? '请查看本批结果' : '开始执行';
    }
    if (loadDownloadedMapping) loadDownloadedMapping.disabled = !gaiaBackendReady;
    if (uploadMapping && mappingPicker?.files?.length) uploadMapping.disabled = !gaiaBackendReady;
  }

  stopBtn?.addEventListener('click', async () => {
    if (stopBtn.disabled) return;
    if (preparing && !uploadController.isBusy()) {
      stopRequested = true;
      refreshGaiaBackendControls();
      return;
    }
    const result = await uploadController.stop();
    if (!result.success) toast(result.message, true);
  });

  function ensureContinuing() {
    if (stopRequested) {
      const error = new Error('运营已停止任务');
      error.name = 'GaiaStopped';
      throw error;
    }
  }

  function clearModelResolution(message) {
    matchedModelSignature = '';
    const skuOutput = $('#gaiaSkuIds');
    if (skuOutput) skuOutput.value = '';
    const output = $('#gaiaModelMatchResult');
    if (output && message) output.textContent = message;
  }

  function operatorModelRows() {
    return ($('#gaiaModels')?.value || '').split(/\r?\n/).map(value => value.trim())
      .filter(Boolean).map(line => {
        const parts = line.split(/[,，\t|]+/).map(value => value.trim()).filter(Boolean);
        if (parts.length >= 2) {
          return {brand: parts.shift(), model: parts.join(' ')};
        }
        return {brand: '', model: line};
      });
  }

  function currentModelSignature() {
    return JSON.stringify({mapping_version_id: mappingVersionId, models: operatorModelRows(),
      quality_labels: selectedQualities()});
  }

  async function resolveOperatorModels(silent) {
    const rows = operatorModelRows();
    if (!mappingVersionId) {
      if (!silent) toast('请先加载并校验型号映射表', true);
      return false;
    }
    if (!rows.length) {
      if (!silent) toast('请先填写至少一个手机型号', true);
      return false;
    }
    const signature = currentModelSignature();
    resolveModelsBtn.disabled = true;
    resolveModelsBtn.textContent = '正在匹配…';
    const resultBox = $('#gaiaModelMatchResult');
    resultBox.textContent = '正在使用当前国行映射版本匹配 ' + rows.length + ' 个型号…';
    const res = await fetch('/api/gaia/model-mappings/resolve-batch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mapping_version_id: mappingVersionId, models: rows,
        quality_labels: selectedQualities(), allow_partial: true})
    }).then(async response => {
      try { return await response.json(); }
      catch (e) { return {success: false, message: '型号匹配响应无效'}; }
    }).catch(() => ({success: false, message: '型号匹配请求失败'}));
    resolveModelsBtn.disabled = false;
    resolveModelsBtn.textContent = '匹配型号';
    const lines = (res.results || []).map(item => {
      const prefix = '第 ' + item.index + ' 行 ' + (item.brand_input ? item.brand_input + ' ' : '') + item.model_input;
      if (item.unique_model) {
        const hit = item.candidates[0];
        return '✓ ' + prefix + ' → ' + hit.brand + ' ' + hit.model + ' · '
          + item.candidate_count + ' 个国行标品 ID 填入'
          + (item.excluded_sku_ids?.length
            ? '；排除盖亚此前拒绝的 ID：' + item.excluded_sku_ids.join('、') : '');
      }
      if (item.excluded_sku_ids?.length && !item.candidate_count) {
        return '✕ ' + prefix + ' → 所有标品 ID 均曾被盖亚明确拒绝：'
          + item.excluded_sku_ids.join('、');
      }
      if (item.candidate_count) {
        const names = item.candidates.slice(0, 4).map(hit =>
          hit.brand + ' ' + hit.model + '（' + hit.standard_sku_id + '）');
        const more = item.candidate_count > names.length
          ? '（共 ' + item.candidate_count + ' 个，仅展示前 ' + names.length + ' 个）'
          : '（共 ' + item.candidate_count + ' 个）';
        return '✕ ' + prefix + ' → 不同品牌或型号存在歧义' + more + '：' + names.join('、');
      }
      return '✕ ' + prefix + ' → 当前国行映射表未找到';
    });
    if (!res.success || signature !== currentModelSignature()) {
      clearModelResolution(lines.join('\n') || res.message || '型号匹配失败');
      if (!silent) toast(res.message || '存在未唯一匹配的型号', true);
      refreshGaiaReadiness();
      return false;
    }
    matchedModelSignature = signature;
    $('#gaiaSkuIds').value = (res.selected_sku_ids || []).join('\n');
    resultBox.textContent = lines.join('\n') + '\n' + res.message
      + (res.deduplicated_count ? '（重复项已去重 ' + res.deduplicated_count + ' 个）' : '');
    if (!silent) toast(res.message);
    refreshGaiaReadiness();
    return true;
  }

  function applyMappingVersion(mapping) {
    if (mappingVersionId && mappingVersionId !== mapping.id) {
      clearModelResolution('型号映射版本已变化，请重新匹配手机型号。');
    }
    mappingVersionId = mapping.id;
    safeMainlandMappingReady = Boolean(mapping.mainland_filter_applied
      && mapping.mapping_policy === 'explicit_mainland_only_v2');
    const excluded = Number(mapping.excluded_non_mainland_sku_count || 0);
    const filterText = mapping.mainland_filter_applied
      ? ' · 已剔除 ' + excluded + ' 个非国行 SKU'
      : '';
    $('#gaiaMappingStatus').textContent = '已校验 · ' + mapping.record_count + ' 个国行标品 SKU' + filterText;
    $('#gaiaMappingStatus').className = 'status-chip ready';
    $('#gaiaSelectedMappingFile').textContent = mapping.name + ' · 版本 ' + mapping.id.slice(0, 8)
      + ' · 更新于 ' + versionTime(mapping.uploaded_at)
      + ' · SHA-256 ' + mapping.sha256.slice(0, 12)
      + (mapping.mainland_filter_applied
        ? ' · 非国行行数 ' + Number(mapping.excluded_non_mainland_rows || 0)
        : '');
    mappingVersionLabel = '映射表 ' + mapping.id.slice(0, 8) + '（' + mapping.name + '，' + versionTime(mapping.uploaded_at) + '）';
    showInputVersions();
    refreshGaiaReadiness();
    refreshGaiaBackendControls();
  }

  function generationPayload() {
    const selectedSkuIds = ($('#gaiaSkuIds')?.value || '').split(/[\s,，;；]+/).filter(Boolean);
    return {
      source_path: selectedSourcePath,
      mapping_version_id: mappingVersionId,
      selected_sku_ids: selectedSkuIds,
      models: operatorModelRows(),
      allow_partial: true,
      shop_id: $('#gaiaShop')?.value || '',
      scene: $('#gaiaScene')?.value || '2',
      quality_link: $('#gaiaQualityLink')?.value || '1',
      quality_labels: selectedQualities(),
      create_count: 1
    };
  }

  function showPreview(preview) {
    const lines = (preview.rows || []).map(row =>
      '第 ' + row.excel_row + ' 行  ' + row.brand + ' ' + row.model
      + '  ·  标品 ' + row.values[3] + '  ·  成色 ID ' + row.values[4]
      + '  ·  策略 ' + (row.values[6] || '留空'));
    const skipped = preview.skipped_goods_combinations || [];
    const skippedLines = skipped.slice(0, 20).map(item =>
      '跳过 标品 ' + item.standard_sku_id + ' · ' + item.quality_label
      + '：' + (item.reason === 'gaia_rejected' ? '盖亚此前明确拒绝此组合' : '原表没有对应成色的货品 ID'));
    const fileCount = Number(preview.file_count || 1);
    const liveCounts = (preview.live_goods_snapshot || []).map(item =>
      item.brand + ' ' + item.model + '：盖亚当前 ' + item.total_count + ' 个货品');
    const fileLines = (preview.files || []).map(item =>
      '第 ' + item.index + ' 份：' + item.row_count + ' 行 · ' + (item.brand || item.models?.[0] || '未知型号')
      + (item.model ? ' ' + item.model : '')
      + (item.chunk_total > 1 ? ' · 该型号第 ' + item.chunk_index + '/' + item.chunk_total + ' 份' : ''));
    const splitText = fileCount > 1
      ? ' · 按盖亚实时货品量自动分成 ' + fileCount + ' 份依次上传' : '';
    $('#gaiaGeneratedPreview').textContent =
      '共 ' + preview.row_count + ' 行 · 质检链接 ' + (preview.quality_link === '0' ? '否（0）' : '是（1）')
      + ' · 成色 ' + (preview.quality_labels || [preview.quality_label]).join('、')
      + '（ID ' + (preview.quality_ids || [preview.quality_id]).join('、') + '）· 映射版本 ' + preview.mapping_version_id.slice(0, 8)
      + splitText + '\n' + lines.join('\n')
      + (liveCounts.length ? '\n实时货品量：' + liveCounts.join('；') : '')
      + (fileLines.length ? '\n分表计划：\n' + fileLines.join('\n') : '')
      + (preview.truncated ? '\n仅展示前 20 行；批次记录包含全部行。' : '')
      + (fileCount > 1 ? '\n系统将自动逐份上传并逐份回查；当前下载链接仅对应第 1 份。' : '')
      + (skipped.length ? '\n已跳过 ' + skipped.length + ' 个缺少对应货品的标品/成色组合：\n'
        + skippedLines.join('\n') + (skipped.length > 20 ? '\n仅展示前 20 个，批次记录包含全部。' : '') : '');
  }

  function refreshGaiaReadiness() {
    syncActivityField();
    const sourceReady = Boolean(selectedSourcePath);
    const mappingReady = Boolean(mappingVersionId);
    const qualities = selectedQualities();
    const conditionSelected = qualities.length > 0;
    const skuSelected = generationPayload().selected_sku_ids.length > 0;
    const modelRows = operatorModelRows();
    const modelsMatched = modelRows.length > 0 && matchedModelSignature === currentModelSignature();
    const unavailableQuality = qualities.find(label => qualityStatus[label]?.configured === false);
    const shopSelected = Boolean($('#gaiaShop')?.value);
    const ready = sourceReady && mappingReady && conditionSelected && skuSelected
      && modelsMatched && shopSelected;
    const key = JSON.stringify(generationPayload());
    if (previewKey && previewKey !== key) {
      previewKey = '';
      previewMeta = null;
      generatedBatchId = '';
      uploadController.setBatch(null);
      downloadLink.hidden = true;
      cancelBtn.hidden = true;
      $('#gaiaGeneratedPreview').textContent = '参数已变化，请重新预览后再生成。';
    }
    previewBtn.disabled = !ready || unavailableQuality;
    generateBtn.disabled = !ready || unavailableQuality || previewKey !== key || Boolean(generatedBatchId);
    handoffBtn.disabled = !generatedBatchId;
    cancelBtn.disabled = !generatedBatchId;
    refreshGaiaBackendControls();
    const status = $('#gaiaGenerationStatus');
    if (!status) return;
    if (preparing && stopRequested) return;
    if (unavailableQuality) {
      status.textContent = '成色 ID 未配置 · 禁止生成';
      status.className = 'status-chip error';
      $('#gaiaGeneratedPreview').textContent = qualityStatus[unavailableQuality].message;
    } else if (generatedBatchId) {
      status.textContent = uploadController.isBusy() ? '盖亚处理中' : '文件已生成';
      status.className = 'status-chip ready';
    } else if (ready) {
      status.textContent = previewKey === key ? '校验通过' : '可以开始';
      status.className = 'status-chip ' + (previewKey === key ? 'ready' : 'neutral');
    } else if (sourceReady || mappingReady || conditionSelected || skuSelected || modelRows.length || shopSelected) {
      status.textContent = shopSelected && conditionSelected && modelRows.length ? '可以开始' : '待填写';
      status.className = 'status-chip neutral';
    } else {
      status.textContent = '待填写';
      status.className = 'status-chip neutral';
    }
  }

  resolveModelsBtn?.addEventListener('click', () => resolveOperatorModels(false));
  $('#gaiaModels')?.addEventListener('input', () => {
    if (matchedModelSignature && matchedModelSignature !== currentModelSignature()) {
      clearModelResolution('手机型号已变化，下次开始时会重新匹配。');
    }
    refreshGaiaReadiness();
  });

  async function inspectServerPath(path, name, size, requestId) {
    const status = $('#gaiaSourceStatus');
    const summary = $('#gaiaInspectSummary');
    status.textContent = '正在识别';
    summary.textContent = '正在读取工作表和表头…';
    const res = await fetch('/api/gaia/excel/inspect', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path})
    }).then(r => r.json()).catch(() => ({success: false, message: '请求失败'}));
    if (requestId !== templateRequestSeq) return false;
    if (!res.success) {
      selectedSourcePath = '';
      status.textContent = '识别失败'; status.className = 'status-chip error';
      summary.textContent = res.message || '文件结构识别失败';
      refreshGaiaReadiness();
      return false;
    }
    const data = res.inspection || {};
    const matches = data.profile_matches || [];
    const profile = matches.length ? matches[0].name : '未知类型';
    const isTemplate = data.status === 'recognized' && matches.some(x => x.id === 'gaia_publish_template');
    selectedSourcePath = isTemplate ? path : '';
    status.textContent = isTemplate ? '结构已识别' : '需要正确模板';
    status.className = 'status-chip ' + (isTemplate ? 'ready' : 'error');
    $('#gaiaSelectedFile').textContent = name + ' · ' + Math.max(1, Math.round((size || 0) / 1024)) + ' KB';
    summary.textContent = '识别结果：' + profile + '\n工作表：' + (data.sheets || []).map(x => x.name).join('、')
      + (isTemplate ? '\n模板结构已确认；只填写新副本，不修改原文件。' : '\n不是唯一识别的盖亚双维成色发品模板，禁止进入生成阶段。');
    refreshGaiaReadiness();
    return isTemplate;
  }

  async function selectLatestTemplate() {
    const requestId = ++templateRequestSeq;
    choose.disabled = true; choose.textContent = '正在检查…';
    const res = await fetch('/api/gaia/templates/current').then(r => r.json())
      .catch(() => ({success: false, message: '读取模板版本失败'}));
    if (requestId !== templateRequestSeq) return false;
    choose.disabled = false; choose.textContent = '检查当前模板';
    if (!res.success || !res.template) {
      selectedSourcePath = '';
      $('#gaiaSourceStatus').textContent = '缺少模板';
      $('#gaiaSourceStatus').className = 'status-chip error';
      $('#gaiaSelectedFile').textContent = res.message || '尚未保存有效模板，请在高级设置上传';
      templateVersionLabel = '模板未就绪';
      showInputVersions();
      refreshGaiaReadiness();
      return false;
    }
    const template = res.template;
    const valid = await inspectServerPath(template.path, template.name, template.size, requestId);
    if (valid) {
      $('#gaiaSelectedFile').textContent = template.name + ' · 版本 '
        + template.id.slice(0, 8) + ' · 更新于 ' + versionTime(template.uploaded_at);
      templateVersionLabel = '模板 ' + template.id.slice(0, 8) + '（' + template.name + '，' + versionTime(template.uploaded_at) + '）';
      showInputVersions();
    }
    return valid;
  }
  choose.addEventListener('click', selectLatestTemplate);
  chooseTemplate?.addEventListener('click', () => templatePicker?.click());
  templatePicker?.addEventListener('change', async () => {
    const file = templatePicker.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    chooseTemplate.disabled = true;
    $('#gaiaSourceStatus').textContent = '正在上传校验';
    const res = await fetch('/api/gaia/templates', {method: 'POST', body: form})
      .then(r => r.json()).catch(() => ({success: false, message: '模板上传失败'}));
    chooseTemplate.disabled = false;
    templatePicker.value = '';
    if (!res.success) {
      toast(res.message || '模板未通过校验，保留上一版本', true);
      await selectLatestTemplate();
      return;
    }
    await selectLatestTemplate();
    toast('发品模板已校验并保存为当前版本');
  });

  if (condition) {
    condition.addEventListener('change', () => {
      if (selectedQualities().length) {
        toast('已选择“' + selectedQualities().join('、') + '”。预览时会校验每种成色的准确 ID。');
      }
      refreshGaiaReadiness();
    });
  }

  if (mappingPicker && chooseMapping) {
    chooseMapping.addEventListener('click', () => mappingPicker.click());
    mappingPicker.addEventListener('change', () => {
      const file = mappingPicker.files && mappingPicker.files[0];
      const status = $('#gaiaMappingStatus');
      const output = $('#gaiaSelectedMappingFile');
        if (!file) {
          mappingVersionId = '';
          safeMainlandMappingReady = false;
          mappingChosenManually = false;
        output.textContent = '尚未选择映射表';
        status.textContent = '尚未加载';
        status.className = 'status-chip neutral';
        if (uploadMapping) uploadMapping.disabled = true;
        refreshGaiaReadiness();
        refreshGaiaBackendControls();
        return;
      }
      const valid = /\.(xlsx|csv)$/i.test(file.name) && !file.name.startsWith('~$');
      mappingChosenManually = true;
      mappingVersionId = '';
      safeMainlandMappingReady = false;
      output.textContent = file.name + ' · ' + Math.max(1, Math.round(file.size / 1024)) + ' KB';
      status.textContent = valid ? '待上传校验' : '格式不支持';
      status.className = 'status-chip ' + (valid ? 'neutral' : 'error');
      if (uploadMapping) uploadMapping.disabled = !valid || !gaiaBackendReady;
      if (!valid) toast('型号映射表仅支持 .xlsx 或 .csv', true);
      refreshGaiaReadiness();
      refreshGaiaBackendControls();
    });
  }
  if (loadDownloadedMapping) {
    async function loadCurrentMapping() {
      if (!gaiaBackendReady) {
        toast('盖亚后端尚未加载国行筛选合同，暂停导入', true);
        return false;
      }
      const status = $('#gaiaMappingStatus');
      loadDownloadedMapping.disabled = true;
      loadDownloadedMapping.textContent = '正在检查…';
      status.textContent = '正在读取当前版本';
      status.className = 'status-chip neutral';
      mappingError = '';
      const res = await fetch('/api/gaia/model-mappings/latest').then(r => r.json())
        .catch(() => ({success: false, message: '读取映射版本失败'}));
      loadDownloadedMapping.disabled = false;
      loadDownloadedMapping.textContent = '检查当前映射表';
      if (!res.success || !res.mapping || !res.mapping.mainland_filter_applied
          || res.mapping.mapping_policy !== 'explicit_mainland_only_v2') {
        mappingVersionId = '';
        safeMainlandMappingReady = false;
        status.textContent = '缺少有效映射';
        status.className = 'status-chip error';
        mappingError = res.message || '没有可用的国行价格规则映射表，请在高级设置上传原表';
        $('#gaiaSelectedMappingFile').textContent = mappingError;
        mappingVersionLabel = '映射表未就绪';
        showInputVersions();
        refreshGaiaReadiness();
        return false;
      }
      applyMappingVersion(res.mapping);
      mappingChosenManually = false;
      if (mappingPicker) mappingPicker.value = '';
      return true;
    }
    loadDownloadedMapping.addEventListener('click', loadCurrentMapping);

    if (startBtn) {
      startBtn.addEventListener('click', async () => {
        if (preparing || uploadController.isBusy()
            || (generatedBatchId && !uploadController.canStart())) return;
        if (!gaiaBackendReady || !gaiaSubmitReady) {
          toast('盖亚启动服务尚不可用，请检查服务状态', true);
          return;
        }
        const payloadBeforeFiles = generationPayload();
        if (!payloadBeforeFiles.shop_id || !payloadBeforeFiles.quality_labels.length
            || !operatorModelRows().length) {
          toast('请先选择目标店铺、成色，并填写手机型号', true);
          return;
        }
        const payment = selectedPayment();
        if (payment.length === 0 || (payment.includes('full') && payment.length > 1)) {
          toast('请选择全款或一个及以上分期期数；全款不能与分期同选', true);
          return;
        }
        const needsActivity = selectedInstallmentTerms().length > 0;
        if (needsActivity && !activityName?.value?.trim()) {
          toast('选择分期时，请填写抖店活动卡片的完整名称', true);
          activityName?.focus();
          return;
        }
        if (needsActivity && activityName.value.includes('全店报名')) {
          toast('仅支持单品/部分商品活动，不能选择全店报名', true);
          activityName.focus();
          return;
        }
        startBtn.disabled = true;
        preparing = true;
        stopRequested = false;
        preparationStopped = false;
        refreshGaiaBackendControls();
        $('#gaiaFormFields').disabled = true;
        let preparationError = '';
        startBtn.textContent = '正在处理…';
        const status = $('#gaiaGenerationStatus');
        const step = (message) => {
          ensureContinuing();
          status.textContent = message;
          status.className = 'status-chip neutral';
          $('#gaiaUploadStatus').textContent = message;
        };
        try {
          // A pre-submit failure retries the same verified batch, never regenerates it.
          if (!generatedBatchId) {
          step('1/6 读取发品模板');
          if (!await selectLatestTemplate()) throw new Error($('#gaiaSelectedFile').textContent || '盖亚空模板识别失败');

          step('2/6 读取国行映射表');
          if (mappingChosenManually && !safeMainlandMappingReady && mappingPicker?.files?.length) {
            if (!await uploadSelectedMapping()) throw new Error(mappingError || '所选映射表校验失败');
          } else if (!mappingChosenManually) {
            if (!await loadCurrentMapping()) throw new Error(mappingError || '直播货架价格规则映射失败');
          } else if (!safeMainlandMappingReady) {
            throw new Error('已保存映射版本未通过国行筛选');
          }

          if (!safeMainlandMappingReady) throw new Error('映射表未通过国行筛选，请使用直播货架价格规则原表');
          step('3/6 自动匹配手机型号');
          if (matchedModelSignature !== currentModelSignature()) {
            if (!await resolveOperatorModels(true)) throw new Error('部分手机型号未找到或有歧义，请查看逐行匹配结果');
          }

          const payload = generationPayload();
          step('4/6 校验填写内容');
          const key = JSON.stringify(payload);
          const previewResponse = await fetch('/api/gaia/batches/preview', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: key
          }).then(r => r.json()).catch(() => ({success: false, message: '预览请求失败'}));
          if (!previewResponse.success) throw new Error(previewResponse.message || '预览校验失败');
          previewKey = key;
          previewMeta = previewResponse.preview;
          generatedBatchId = '';
          showPreview(previewMeta);

          step('5/6 生成并回读文件');
          const generated = await fetch('/api/gaia/batches/generate', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({...payload,
              expected_source_sha256: previewMeta.source_sha256,
              expected_quality_link: previewMeta.quality_link,
              expected_quality_ids: previewMeta.quality_ids,
              expected_inventory_probe_token: previewMeta.inventory_probe_token,
              expected_split_plan_sha256: previewMeta.split_plan_sha256})
          }).then(r => r.json()).catch(() => ({success: false, message: '生成请求失败'}));
          if (!generated.success) throw new Error(generated.message || '生成失败');
          generatedBatchId = generated.batch.id;
          uploadController.setBatch(generated.batch);
          showPreview(generated.preview);
          downloadLink.href = generated.download_url;
          downloadLink.hidden = false;
          cancelBtn.hidden = false;
          }

          step('6/6 校验上传条件并启动盖亚');
          const shopId = encodeURIComponent(generationPayload().shop_id);
          const handoff = await fetch('/api/gaia/batches/' + generatedBatchId
            + '/handoff-preflight?shop_id=' + shopId).then(r => r.json())
            .catch(() => ({success: false, message: '上传前校验请求失败'}));
          ensureContinuing();
          if (!handoff.success) {
            const reasons = handoff.preflight?.errors || [handoff.message || '上传前校验失败'];
            throw new Error(reasons.join('；'));
          }
          $('#gaiaGeneratedPreview').textContent += '\n已完成国行筛选、文件生成与上传前校验。批次 '
            + generatedBatchId + '；正在自动上传。';
          const started = await uploadController.start();
          if (!started.success) throw new Error(started.message);
        } catch (error) {
          if (stopRequested) {
            // The generate response may have arrived after Stop. Cancel that
            // exact file before allowing another task; never start its browser.
            if (generatedBatchId) {
              const stopped = await uploadController.stop();
              if (!stopped.success) {
                const view = uploadController.stopStatus();
                status.textContent = view.kind === 'submitted' ? '已进入提交，无法撤回' : '停止未确认';
                status.className = 'status-chip error';
                toast(stopped.message, true);
              }
            } else {
              preparationStopped = true;
              uploadController.setBatch(null);
              $('#gaiaUploadStatus').textContent = '已停止，未上传或提交盖亚。';
              status.textContent = '已停止';
              status.className = 'status-chip neutral';
            }
            return;
          }
          preparationError = error?.message || '盖亚任务处理失败';
          if (!generatedBatchId) uploadController.setBatch(null);
          status.textContent = '任务阻断';
          status.className = 'status-chip error';
          $('#gaiaUploadStatus').textContent = '已停止：' + preparationError + '。未继续上传，请修改后重试。';
          $('#gaiaResultDetails').open = true;
          toast(error?.message || '盖亚任务处理失败', true);
        } finally {
          preparing = false;
          $('#gaiaFormFields').disabled = uploadController.isBusy();
          refreshGaiaBackendControls();
          if (preparationError) {
            status.textContent = '任务已停止';
            status.className = 'status-chip error';
          }
        }
      });
    }
  }
  async function uploadSelectedMapping() {
      if (!gaiaBackendReady) {
        toast('盖亚后端尚未加载国行筛选合同，暂停上传', true);
        return false;
      }
      const file = mappingPicker?.files?.[0];
      if (!file) return false;
      const status = $('#gaiaMappingStatus');
      const output = $('#gaiaSelectedMappingFile');
      const form = new FormData();
      form.append('file', file);
      uploadMapping.disabled = true;
      status.textContent = '正在上传校验';
      status.className = 'status-chip neutral';
      const res = await fetch('/api/gaia/model-mappings', {method: 'POST', body: form})
        .then(r => r.json()).catch(() => ({success: false, message: '上传请求失败'}));
      if (!res.success) {
        mappingVersionId = '';
        safeMainlandMappingReady = false;
        status.textContent = '校验未通过';
        status.className = 'status-chip error';
        uploadMapping.disabled = false;
        mappingError = res.message || '型号映射表校验失败';
        output.textContent = mappingError;
        toast(mappingError, true);
        refreshGaiaBackendControls();
      } else {
        mappingError = '';
        const mapping = res.mapping;
        mappingChosenManually = true;
        applyMappingVersion(mapping);
        toast('型号映射表已上传并校验：' + mapping.record_count + ' 个标品 SKU');
      }
      refreshGaiaReadiness();
      return Boolean(res.success);
  }
  uploadMapping?.addEventListener('click', uploadSelectedMapping);
  selectLatestTemplate().catch(() => {});
  fetch('/api/gaia/model-mappings/latest').then(r => r.json()).then(res => {
    if (preparing || mappingVersionId || mappingChosenManually) return;
    if (!res.success) {
      $('#gaiaMappingStatus').textContent = '版本校验失败';
      $('#gaiaMappingStatus').className = 'status-chip error';
      $('#gaiaSelectedMappingFile').textContent = res.message || '已保存的映射版本不可用，请重新上传';
      mappingVersionLabel = '映射表未就绪';
      showInputVersions();
      refreshGaiaReadiness();
      return;
    }
    if (!res.mapping) {
      mappingVersionLabel = '映射表未就绪';
      showInputVersions();
      $('#gaiaSelectedMappingFile').textContent = '尚未保存有效映射表，请在高级设置上传';
      return;
    }
    if (mappingPicker?.files?.length) return;
    const mapping = res.mapping;
    if (/\u6296\u97f3\u76f4\u64ad\u8d27\u67b6\u8d27\u54c1\u4ef7\u683c\u89c4\u5219\u6570\u636e/.test(mapping.name || '')
        && !mapping.mainland_filter_applied) {
      mappingVersionId = '';
      safeMainlandMappingReady = false;
      $('#gaiaMappingStatus').textContent = '需重新导入 · 尚未执行国行筛选';
      $('#gaiaMappingStatus').className = 'status-chip error';
      $('#gaiaSelectedMappingFile').textContent = mapping.name + ' · 请上传新版国行价格规则原表';
      mappingVersionLabel = '映射表未就绪';
      showInputVersions();
      refreshGaiaReadiness();
      refreshGaiaBackendControls();
      return;
    }
    applyMappingVersion(mapping);
  }).catch(() => {});
  fetch('/api/gaia/qualities/status').then(r => r.json()).then(res => {
    if (res.success) {
      qualityStatus = res.qualities || {};
      refreshGaiaReadiness();
    }
  }).catch(() => {});
  fetch('/api/gaia/capabilities').then(r => r.json()).then(res => {
    gaiaSubmitReady = Boolean(res.success && res.features?.gaia_submit
      && res.contracts?.payment_terms_policy === 'explicit_terms_v1');
    uploadController.setEnabled(gaiaSubmitReady);
    gaiaBackendReady = res.success
      && res.contracts?.mapping_policy === 'explicit_mainland_only_v2';
    refreshGaiaBackendControls();
    if (!gaiaSubmitReady && gaiaBackendReady) {
      $('#gaiaGenerationStatus').textContent = '等待面板服务重启以启用多选分期';
      $('#gaiaGenerationStatus').className = 'status-chip neutral';
    } else if (!gaiaBackendReady && !safeMainlandMappingReady) {
      $('#gaiaGenerationStatus').textContent = '等待面板服务重启';
      $('#gaiaGenerationStatus').className = 'status-chip neutral';
    }
  }).catch(() => {
    gaiaBackendReady = false;
    refreshGaiaBackendControls();
  });

  for (const selector of ['#gaiaShop', '#gaiaScene', '#gaiaQualityLink', '#gaiaCreateCount']) {
    $(selector)?.addEventListener('change', refreshGaiaReadiness);
    $(selector)?.addEventListener('input', refreshGaiaReadiness);
  }
  previewBtn.addEventListener('click', async () => {
    uploadController.setBatch(null);
    const payload = generationPayload();
    const key = JSON.stringify(payload);
    previewBtn.disabled = true;
    const res = await fetch('/api/gaia/batches/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: key
    }).then(r => r.json()).catch(() => ({success: false, message: '预览请求失败'}));
    if (!res.success) {
      previewKey = '';
      previewMeta = null;
      generatedBatchId = '';
      cancelBtn.hidden = true;
      $('#gaiaGeneratedPreview').textContent = res.message || '预览校验失败';
      toast(res.message || '预览校验失败', true);
    } else {
      previewKey = key;
      previewMeta = res.preview;
      generatedBatchId = '';
      cancelBtn.hidden = true;
      showPreview(res.preview);
      downloadLink.hidden = true;
    }
    refreshGaiaReadiness();
  });
  generateBtn.addEventListener('click', async () => {
    uploadController.setBatch(null);
    const payload = generationPayload();
    if (JSON.stringify(payload) !== previewKey || !previewMeta) return;
    generateBtn.disabled = true;
    const res = await fetch('/api/gaia/batches/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...payload,
        expected_source_sha256: previewMeta.source_sha256,
        expected_quality_link: previewMeta.quality_link,
        expected_quality_ids: previewMeta.quality_ids,
        expected_inventory_probe_token: previewMeta.inventory_probe_token,
        expected_split_plan_sha256: previewMeta.split_plan_sha256})
    }).then(r => r.json()).catch(() => ({success: false, message: '生成请求失败'}));
    if (!res.success) {
      previewKey = '';
      previewMeta = null;
      generatedBatchId = '';
      downloadLink.hidden = true;
      cancelBtn.hidden = true;
      $('#gaiaGeneratedPreview').textContent = res.message || '生成失败';
      toast(res.message || '生成失败', true);
    } else {
      showPreview(res.preview);
      generatedBatchId = res.batch.id;
      uploadController.setBatch(res.batch);
      cancelBtn.hidden = false;
      downloadLink.href = res.download_url;
      downloadLink.hidden = false;
      toast('盖亚文件已生成并回读校验；尚未上传盖亚。');
    }
    refreshGaiaReadiness();
  });
  handoffBtn.addEventListener('click', async () => {
    if (!generatedBatchId) return;
    handoffBtn.disabled = true;
    const shopId = encodeURIComponent($('#gaiaShop')?.value || '');
    const url = '/api/gaia/batches/' + generatedBatchId + '/handoff-preflight?shop_id=' + shopId;
    const res = await fetch(url).then(r => r.json())
      .catch(() => ({success: false, message: '交接检查请求失败'}));
    if (!res.success) {
      const reasons = res.preflight?.errors || [res.message || '交接检查失败'];
      $('#gaiaGeneratedPreview').textContent += '\n上传前检查未通过：' + reasons.join('；');
      toast('上传前检查未通过', true);
    } else {
      $('#gaiaGeneratedPreview').textContent += '\n文件交接检查通过；分步检查不会提交，可点击“开始执行”继续上传。';
      toast('文件交接检查通过；尚未上传盖亚。');
    }
    refreshGaiaReadiness();
  });
  cancelBtn.addEventListener('click', async () => {
    if (!generatedBatchId) return;
    const batchId = generatedBatchId;
    const shopId = $('#gaiaShop')?.value || '';
    const reason = window.prompt('请填写取消原因（至少 2 个字）。仅取消本地未提交批次，不会撤销盖亚网页操作。');
    if (reason === null) return;
    if (reason.trim().length < 2 || reason.trim().length > 300) {
      toast('取消原因需填写 2～300 字', true); return;
    }
    if (!window.confirm('确认取消本地批次 ' + batchId + '？生成文件会保留供核查。')) return;
    cancelBtn.disabled = true;
    const res = await fetch('/api/gaia/batches/' + encodeURIComponent(batchId) + '/cancel', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({shop_id: shopId, reason: reason.trim()})
    }).then(r => r.json()).catch(() => ({success: false, message: '取消请求失败'}));
    if (!res.success) {
      toast(res.message || '取消失败，请刷新批次状态后核查', true);
      cancelBtn.disabled = false;
      return;
    }
    generatedBatchId = '';
    cancelBtn.hidden = true;
    uploadController.setBatch(null);
    downloadLink.hidden = true;
    $('#gaiaGeneratedPreview').textContent += '\n本地批次已取消（ID ' + batchId + '）；未向盖亚提交。';
    toast('本地批次已取消；未向盖亚提交。');
    refreshGaiaReadiness();
    $('#gaiaGenerationStatus').textContent = '本批已取消 · 可重新生成';
    $('#gaiaGenerationStatus').className = 'status-chip neutral';
  });
};
