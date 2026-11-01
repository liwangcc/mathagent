// Exact lesson identity, not title keywords: a custom lesson must never borrow
// an unrelated chapter illustration (or a different education stage's cover).
export function coursewareCoverForCard(card, manifest) {
  const stage = card?.stage || card?.education_stage
  const fileName = card?.file_name || card?.offline_file_name || card?.offlineFileName
  const candidates = Object.values(manifest).filter(cover => cover.stage === stage)
  if (fileName) return candidates.find(cover => cover.fileName === fileName) || null
  return candidates.find(cover => cover.chapter === card?.chapter) || null
}

// Unknown/new lessons use their own HTML, isolated exactly like the full preview.
// The thumbnail additionally disallows all network requests and parent access.
export function coursewareThumbnailHtml(html) {
  const head = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'">
<style>html,body{margin:0!important;background:#f6f8f4!important}*{animation:none!important;transition:none!important}</style>`
  const focus = `<script>(()=>{
    let attempts=0;
    function focus(){
      const graphics=[...document.querySelectorAll('svg,canvas')].filter(e=>{
        const r=e.getBoundingClientRect();return r.width>200&&r.height>100&&e.checkVisibility();
      });
      const main=graphics.find(e=>['diagram','plot','chart','s'].includes(e.id))||graphics[0];
      if(!main){if(++attempts<20)setTimeout(focus,100);return;}
      const parent=main.parentElement;
      const target=graphics.some(e=>e.tagName==='CANVAS'&&e.parentElement===parent)?parent:main;
      document.body.style.visibility='hidden';
      Object.assign(target.style,{visibility:'visible',position:'fixed',left:'0',top:'0',width:'800px',height:'450px',margin:'0',background:'#f6f8f4'});
      target.querySelectorAll('button,input,select').forEach(e=>e.style.visibility='hidden');
    }
    if(document.readyState==='complete')setTimeout(focus,150);
    else window.addEventListener('load',()=>setTimeout(focus,150),{once:true});
  })();<\/script>`
  const source = String(html || '')
  const isolated = /<head\b[^>]*>/i.test(source)
    ? source.replace(/<head\b[^>]*>/i, match => match + head)
    : head + source
  return /<\/body\s*>/i.test(isolated)
    ? isolated.replace(/<\/body\s*>/i, focus + '</body>')
    : isolated + focus
}
