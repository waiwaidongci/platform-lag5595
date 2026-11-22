import {feedback, field} from './fields.js';
import {panel, pill} from './panels.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, p} from '../utils/dom.js';

const ENDPOINTS = [
  {key: 'start', label: '首次对时（批次首端）'},
  {key: 'end', label: '末次对时（批次末端）'},
];

function syncForm(datasetId, endpoint, current, refresh) {
  const message = feedback();
  const hidden = el('input', {type: 'hidden', name: 'version', value: current ? current.version : ''});
  const submit = button(current ? '更正对时记录' : '保存对时记录',
    {type: 'submit', class: 'primary', 'data-testid': `sync-${endpoint.key}`});
  const form = el('form', {class: 'fields sync-form',
    onSubmit: handler(submit, message, async event => {
      const data = new FormData(event.target);
      const versionValue = data.get('version');
      const payload = {
        endpoint: endpoint.key,
        observed_at: data.get('observed_at'),
        deviation_low: Number(data.get('deviation_low')),
        deviation_high: Number(data.get('deviation_high')),
        note: data.get('note') || '',
      };
      if (versionValue !== '') payload.version = Number(versionValue);
      await workspace.saveClockSync(datasetId, payload);
      await refresh();
    })},
    hidden,
    field({key: 'observed_at', label: '对时观测时刻（YYYY-MM-DD HH:MM:SS）', type: 'string',
      max_length: 30}, current ? current.observed_at : ''),
    el('div', {class: 'form-grid'},
      field({key: 'deviation_low', label: '偏差下限（秒）', type: 'number', min: -600, max: 600},
        current ? current.deviation_low : ''),
      field({key: 'deviation_high', label: '偏差上限（秒）', type: 'number', min: -600, max: 600},
        current ? current.deviation_high : '')),
    field({key: 'note', label: '备注', type: 'string', required: false, max_length: 300},
      current ? current.note : ''),
    el('div', {class: 'actions'}, submit), message);
  return form;
}

function syncCard(datasetId, endpoint, current, refresh) {
  const meta = current
    ? el('p', {class: 'subtle'},
        `版本 v${current.version} · ${current.author} · ${current.updated_at || ''}`,
        pill(`偏差 ${current.deviation_low}～${current.deviation_high} 秒`, 'ok'))
    : p('尚未录入，偏差区间缺失时该端按未对时处理。', 'subtle');
  return el('div', {class: 'sync-card'},
    el('h3', {}, endpoint.label), meta,
    session.canWrite ? syncForm(datasetId, endpoint, current, refresh)
      : p(current ? '只读角色可查看对时记录。' : '暂无记录。', 'subtle'));
}

export async function clockSyncPanel(datasetId, refresh) {
  let syncs = [];
  try {
    syncs = await workspace.clockSyncs(datasetId);
  } catch (error) {
    return panel('首尾对时', p(error.message, 'feedback'));
  }
  const byEndpoint = Object.fromEntries(syncs.map(item => [item.endpoint, item]));
  const complete = ['start', 'end'].every(key => byEndpoint[key]);
  return panel('首尾对时与漂移区间',
    p('偏差 = 声级计时刻 − 基准时刻（秒）。每端只确定一个偏差区间，区间内任意起止偏差都可能成立，'
      + '中间按匀速漂移；运行排队时快照，扫描重试不更换。', 'subtle'),
    pill(complete ? '首尾对时齐全，运行将枚举漂移场景' : '对时记录不齐全，运行按零偏差处理',
      complete ? 'ok' : 'warning'),
    ...ENDPOINTS.map(endpoint => syncCard(datasetId, endpoint, byEndpoint[endpoint.key], refresh)));
}
