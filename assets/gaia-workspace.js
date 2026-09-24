/* Gaia workspace orchestration only; never starts Doudian tasks. */
window.initGaiaWorkspace = function ({$, toast}) {
  const choose = $('#gaiaChooseFile');
  if (!choose) return;
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
  let preparing = false;
  let stopRequested = false;
  let preparationStopped = false;
  let mappingChosenManually = false;
  let gaiaSubmitReady = false;
  let mappingError = '';
  let discovery = null;
  let selectedSourcePath = '';
  let mappingVersionId = '';
  let previewKey = '';
  let previewMeta = null;
  let generatedBatchId = '';
  let qualityStatus = {};
  let gaiaBackendReady = false;
  let safeMainlandMappingReady = false;
  let matchedModelSignature = '';
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
        : busy ? '盖亚处理中' : accepted ? '盖亚已受理' : '请查看执行结果';
      status.className = 'status-chip ' + (busy || batch.state === 'cancelled' ? 'neutral' : accepted ? 'ready' : 'error');
    }
    refreshGaiaBackendControls();
  }});
  if (loadDownloadedMapping) loadDownloadedMapping.disabled = true;
  if (uploadMapping) uploadMapping.disabled = true;

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
      startBtn.disabled = preparing || busy || processed || !gaiaBackendReady || !gaiaSubmitReady;
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
    return JSON.stringify({mapping_version_id: mappingVersionId, models: operatorModelRows()});
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
      body: JSON.stringify({mapping_version_id: mappingVersionId, models: rows})
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
          + item.candidate_count + ' 个国行标品 ID 全部填入';
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
      + ' · SHA-256 ' + mapping.sha256.slice(0, 12)
      + (mapping.mainland_filter_applied
        ? ' · 非国行行数 ' + Number(mapping.excluded_non_mainland_rows || 0)
        : '');
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
      shop_id: $('#gaiaShop')?.value || '',
      scene: $('#gaiaScene')?.value || '2',
      quality_label: condition?.value || '',
      create_count: 1
    };
  }

  function showPreview(preview) {
    const lines = (preview.rows || []).map(row =>
      '第 ' + row.excel_row + ' 行  ' + row.brand + ' ' + row.model
      + '  ·  标品 ' + row.values[3] + '  ·  策略 ' + (row.values[6] || '留空'));
    $('#gaiaGeneratedPreview').textContent =
      '共 ' + preview.row_count + ' 行 · 成色 ' + preview.quality_label
      + '（ID ' + preview.quality_id + '）· 映射版本 ' + preview.mapping_version_id.slice(0, 8)
      + '\n' + lines.join('\n') + (preview.truncated ? '\n仅展示前 20 行；下载文件包含全部行。' : '');
  }

  function refreshGaiaReadiness() {
    const sourceReady = Boolean(selectedSourcePath);
    const mappingReady = Boolean(mappingVersionId);
    const conditionSelected = Boolean(condition?.value);
    const skuSelected = generationPayload().selected_sku_ids.length > 0;
    const modelRows = operatorModelRows();
    const modelsMatched = modelRows.length > 0 && matchedModelSignature === currentModelSignature();
    const unavailableQuality = conditionSelected && qualityStatus[condition.value]?.configured === false;
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
      $('#gaiaGeneratedPreview').textContent = qualityStatus[condition.value].message;
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

  async function inspectServerPath(path, name, size) {
    const status = $('#gaiaSourceStatus');
    const summary = $('#gaiaInspectSummary');
    status.textContent = '正在识别';
    summary.textContent = '正在读取工作表和表头…';
    const res = await fetch('/api/gaia/excel/inspect', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path})
    }).then(r => r.json()).catch(() => ({success: false, message: '请求失败'}));
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
    choose.disabled = true; choose.textContent = '正在查找…';
    const res = discovery || await fetch('/api/gaia/excel/latest').then(r => r.json())
      .catch(() => ({success: false, message:'请求失败'}));
    if (preparing) discovery = res;
    choose.disabled = false; choose.textContent = '检查最新模板';
    const templates = (res.files || []).filter(item => item.status === 'recognized'
      && (item.profile_matches || []).some(match => match.id === 'gaia_publish_template'));
    let candidate = templates.find(item => item.source_kind === 'downloads') || templates[0];
    if (!candidate) {
      // macOS LaunchAgent 可能被 TCC 拒绝读取 ~/Downloads。已验证模板保存在
      // 应用受控目录；旧版后端也允许检查 imports 下的路径。
      candidate = {
        path: 'panel_data/gaia_listing/imports/templates/抖音双维成色发品模版20260909165746.xlsx',
        name: '抖音双维成色发品模版20260909165746.xlsx',
        size: 9933
      };
    }
    const valid = await inspectServerPath(candidate.path, candidate.name, candidate.size);
    if (valid && candidate.source_kind !== 'downloads') {
      $('#gaiaSelectedFile').textContent += ' · 使用已保存模板（未读取到下载目录中的模板）';
    }
    return valid;
  }
  choose.addEventListener('click', selectLatestTemplate);

  if (condition) {
    condition.addEventListener('change', () => {
      if (condition.value) {
        toast('已选择“' + condition.value + '”。预览时会校验准确的 dyQualityId。');
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
    async function importLatestDownloadedMapping() {
      if (!gaiaBackendReady) {
        toast('盖亚后端尚未加载国行筛选合同，暂停导入', true);
        return false;
      }
      const status = $('#gaiaMappingStatus');
      loadDownloadedMapping.disabled = true;
      loadDownloadedMapping.textContent = '正在查找…';
      status.textContent = '正在识别下载目录';
      status.className = 'status-chip neutral';
      mappingError = '';
      const latest = discovery || await fetch('/api/gaia/excel/latest').then(r => r.json())
        .catch(() => ({success: false, message: '读取下载目录失败'}));
      const mappings = (latest.files || []).filter(item => item.status === 'recognized'
        && (item.profile_matches || []).some(match => match.id === 'gaia_goods_price_mapping'));
      const candidate = mappings.find(item => item.source_kind === 'downloads') || mappings[0];
      if (!latest.success || !candidate) {
        status.textContent = '未找到匹配表';
        status.className = 'status-chip error';
        loadDownloadedMapping.disabled = false;
        loadDownloadedMapping.textContent = '读取下载目录匹配表';
        mappingError = latest.message || '没有可用的价格规则映射表，请在高级设置上传原表';
        $('#gaiaSelectedMappingFile').textContent = mappingError;
        toast(mappingError, true);
        return false;
      }
      status.textContent = '正在读取并校验大表';
      const res = await fetch('/api/gaia/model-mappings/import-download', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: candidate.path})
      }).then(r => r.json()).catch(() => ({success: false, message: '导入请求失败'}));
      loadDownloadedMapping.disabled = false;
      loadDownloadedMapping.textContent = '读取下载目录匹配表';
      if (!res.success) {
        mappingVersionId = '';
        safeMainlandMappingReady = false;
        status.textContent = '校验未通过';
        status.className = 'status-chip error';
        mappingError = res.message || '匹配表导入失败';
        $('#gaiaSelectedMappingFile').textContent = mappingError;
        toast(mappingError, true);
        refreshGaiaReadiness();
        refreshGaiaBackendControls();
        return false;
      }
      applyMappingVersion(res.mapping);
      mappingChosenManually = false;
      if (mappingPicker) mappingPicker.value = '';
      if (res.reused) {
        $('#gaiaSelectedMappingFile').textContent += ' · 使用已保存版本（未读取到下载目录中的价格表；有更新请手动上传）';
        $('#gaiaMappingStatus').textContent = '已校验 · 复用保存版本';
      }
      return true;
    }
    loadDownloadedMapping.addEventListener('click', importLatestDownloadedMapping);

    if (startBtn) {
      startBtn.addEventListener('click', async () => {
        if (preparing || uploadController.isBusy()
            || (generatedBatchId && !uploadController.canStart())) return;
        if (!gaiaBackendReady || !gaiaSubmitReady) {
          toast('盖亚启动服务尚不可用，请检查服务状态', true);
          return;
        }
        const payloadBeforeFiles = generationPayload();
        if (!payloadBeforeFiles.shop_id || !payloadBeforeFiles.quality_label
            || !operatorModelRows().length) {
          toast('请先选择目标店铺、成色，并填写手机型号', true);
          return;
        }
        startBtn.disabled = true;
        preparing = true;
        stopRequested = false;
        preparationStopped = false;
        refreshGaiaBackendControls();
        discovery = null;
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
          if (!await selectLatestTemplate()) throw new Error($('#gaiaInspectSummary').textContent || '盖亚空模板识别失败');

          step('2/6 读取国行映射表');
          if (mappingChosenManually && !safeMainlandMappingReady && mappingPicker?.files?.length) {
            if (!await uploadSelectedMapping()) throw new Error(mappingError || '所选映射表校验失败');
          } else if (!mappingChosenManually) {
            if (!await importLatestDownloadedMapping()) throw new Error(mappingError || '直播货架价格规则映射失败');
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
              expected_quality_id: previewMeta.quality_id})
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
          discovery = null;
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
  fetch('/api/gaia/model-mappings/latest').then(r => r.json()).then(res => {
    if (preparing || mappingVersionId || mappingChosenManually) return;
    if (!res.success) {
      $('#gaiaMappingStatus').textContent = '版本校验失败';
      $('#gaiaMappingStatus').className = 'status-chip error';
      $('#gaiaSelectedMappingFile').textContent = res.message || '已保存的映射版本不可用，请重新上传';
      refreshGaiaReadiness();
      return;
    }
    if (!res.mapping || mappingPicker?.files?.length) return;
    const mapping = res.mapping;
    if (/\u6296\u97f3\u76f4\u64ad\u8d27\u67b6\u8d27\u54c1\u4ef7\u683c\u89c4\u5219\u6570\u636e/.test(mapping.name || '')
        && !mapping.mainland_filter_applied) {
      mappingVersionId = '';
      safeMainlandMappingReady = false;
      $('#gaiaMappingStatus').textContent = '需重新导入 · 尚未执行国行筛选';
      $('#gaiaMappingStatus').className = 'status-chip error';
      $('#gaiaSelectedMappingFile').textContent = mapping.name + ' · 点击“开始”或“读取下载目录匹配表”重建映射';
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
    gaiaSubmitReady = Boolean(res.success && res.features?.gaia_submit);
    uploadController.setEnabled(gaiaSubmitReady);
    gaiaBackendReady = res.success
      && res.contracts?.mapping_policy === 'explicit_mainland_only_v2';
    refreshGaiaBackendControls();
    if (!gaiaBackendReady && !safeMainlandMappingReady) {
      $('#gaiaGenerationStatus').textContent = '等待面板服务重启';
      $('#gaiaGenerationStatus').className = 'status-chip neutral';
    }
  }).catch(() => {
    gaiaBackendReady = false;
    refreshGaiaBackendControls();
  });

  for (const selector of ['#gaiaShop', '#gaiaScene', '#gaiaCreateCount']) {
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
        expected_quality_id: previewMeta.quality_id})
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
