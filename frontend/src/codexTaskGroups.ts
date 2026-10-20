export function groupWbExports(rows: any[]) {
  const groups = new Map<string, any[]>();
  for (const row of rows) {
    const request = row.payload?.codex_request_id || row.payload?.batch_id;
    const key = row.payload?.generation_provider === 'codex' && request
      ? JSON.stringify([request, row.created_by || '', row.target_worker_id || '', row.mode])
      : row.id;
    const members = groups.get(key) || [];
    members.push(row); groups.set(key, members);
  }
  return [...groups.entries()].map(([key, members]) => {
    if (members.length === 1) return members[0];
    const first = members[0];
    const total = members.reduce((n, row) => n + (row.payload?.product_ids?.length || 1), 0);
    const progress = members.reduce((n, row) => n + (row.payload?.product_ids?.length || 1) *
      (row.status === 'completed' ? 100 : Math.max(0, Math.min(100, row.progress || 0))), 0) / total;
    const status = ['running','retrying','failed','action_required','queued','paused','completed','cancelled']
      .find(state => members.some(row => row.status === state)) || first.status;
    return {...first, id: `codex-${encodeURIComponent(key)}`,
      grouped: true, children: members, status, progress: Math.round(progress),
      payload: {...first.payload, product_ids: members.flatMap(row => row.payload?.product_ids || [])},
      publication_status: new Set(members.map(row => row.publication_status)).size === 1 ? first.publication_status : '部分完成，请展开查看',
      copy_failed_items: members.reduce((n,row)=>n+(row.copy_failed_items||0),0),
      external_product_count: members.reduce((n,row)=>n+(row.external_product_count||0),0),
      external_product_ids: [...new Set(members.flatMap(row=>row.external_product_ids||[]))].slice(0,5),
      download_ready: false, history_report_asset_id: undefined};
  });
}
