import { useState } from 'react'

export type VideoMode = 'text' | 'frames' | 'references' | 'edit'
export type InputAsset = { id: string; name: string; url: string; duration_seconds?: number }
export type Materials = { start?: InputAsset; end?: InputAsset; references: InputAsset[]; video?: InputAsset }
type Media = { id: string; name: string; kind: 'image' | 'video'; url: string }

export default function MaterialInputs({ mode, value, onChange, media, onBusy, referenceSettingsExplicit = false }: {
  referenceSettingsExplicit?: boolean; mode: VideoMode; value: Materials; onChange: (next: Materials) => void; media: Media[]; onBusy: (busy: boolean) => void
}) {
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  async function upload(files: File[], slot: 'start' | 'end' | 'references' | 'video') {
    setError('')
    const video = slot === 'video'
    if (!files.length) return
    if (files.some(f => f.size > (video ? 100 : 20) * 1024 * 1024)) { setError(video ? '视频不能超过 100 MB。' : '单张图片不能超过 20 MB。'); return }
    setUploading(true); onBusy(true)
    const added: InputAsset[] = []
    try {
      for (const file of files) {
        const form = new FormData(); form.append('file', file)
        const r = await fetch(`/console/api/uploads/${video ? 'video' : 'image'}`, { method: 'POST', body: form })
        const data = await r.json()
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '上传失败，请检查文件格式。')
        added.push({ id: data.image_id || data.video_id, name: data.name, url: data.url, duration_seconds: data.duration_seconds })
      }
    } catch (e) { setError(e instanceof Error ? e.message : '上传失败，请重试。') }
    finally {
      // Keep completed uploads if a later file fails. Do not discard user input.
      if (added.length) onChange(slot === 'references' ? { ...value, references: [...value.references, ...added] } : { ...value, [slot]: added[0] })
      setUploading(false); onBusy(false)
    }
  }
  function remove(slot: 'start' | 'end' | 'video' | 'references', id?: string) {
    onChange(slot === 'references' ? { ...value, references: value.references.filter(a => a.id !== id) } : { ...value, [slot]: undefined })
  }
  function preview(asset: InputAsset, slot: 'start' | 'end' | 'video' | 'references') {
    return <div className="input-preview" key={asset.id}>{slot === 'video' ? <video src={asset.url} controls preload="metadata" /> : <img src={asset.url} alt={asset.name} />}<div><strong>{asset.name}</strong><small>已保存在本机{asset.duration_seconds ? ` · ${asset.duration_seconds.toFixed(2)} 秒` : ''}</small><button type="button" className="text-link" disabled={uploading} onClick={() => remove(slot, asset.id)}>移除{asset.name}</button></div></div>
  }
  function picker(slot: 'start' | 'end' | 'video' | 'references', label: string) {
    const video = slot === 'video'
    return <div className="material-picker"><label className="upload"><input aria-label={label} type="file" disabled={uploading} multiple={slot === 'references'} accept={video ? 'video/mp4,.mp4' : 'image/png,image/jpeg,image/webp'} onChange={e => { const files = Array.from(e.target.files || []); e.target.value = ''; void upload(files, slot) }} /><span>↑</span><strong>{uploading ? '正在保存素材…' : label}</strong><small>{video ? 'MP4 · 最长 10 秒 · 最大 100 MB' : 'JPG / PNG / WebP · 每张最大 20 MB'}</small></label><select aria-label={`${label}：从素材库选择`} disabled={uploading} value="" onChange={e => { const item = media.find(m => m.id === e.target.value); if (!item) return; const asset = { id: `media:${item.id}`, name: item.name, url: item.url }; if (slot === 'references') { if (value.references.some(a => a.id === asset.id)) { setError('该参考图已添加。'); return } onChange({ ...value, references: [...value.references, asset] }) } else onChange({ ...value, [slot]: asset }) }}><option value="">或从素材库选择{video ? '原视频' : '图片'}</option>{media.filter(m => m.kind === (video ? 'video' : 'image') && (!video || m.name.toLowerCase().endsWith('.mp4'))).map(m => <option key={m.id} value={m.id}>{m.name}</option>)}</select></div>
  }
  return <section className="material-inputs" aria-label="参考素材">
    <h3>参考素材{mode === 'text' ? '（可选）' : ''}</h3>
    {mode === 'text' && <p>纯文字生成无需素材。上方选择“首尾帧”“参考图”或“视频编辑”，即可上传对应素材。</p>}
    {mode === 'frames' && <><p>首帧必选，尾帧可选；首尾帧控制视频的起点与终点。</p><div className="frame-inputs"><div><h4>首帧图片（必选）</h4>{value.start && preview(value.start, 'start')}{picker('start', value.start ? '替换首帧图片' : '上传首帧图片')}</div><div><h4>尾帧图片（可选）</h4>{value.end && preview(value.end, 'end')}{picker('end', value.end ? '替换尾帧图片' : '上传尾帧图片')}</div></div></>}
    {mode === 'references' && <><p>上传人物、物品或场景参考图，配合提示词生成视频。可继续添加多张图片，后台不额外限制张数；实际可用数量由 Flow 决定，每次生成 1 条。</p><div className="input-preview-list">{value.references.map(a => preview(a, 'references'))}</div>{picker('references', `上传参考图片（已选 ${value.references.length} 张）`)}<p>{referenceSettingsExplicit ? 'Veo 固定 8 秒、720p，使用上方比例。' : '此模式无法指定比例或画质，输出由 Flow 决定。'}</p></>}
    {mode === 'edit' && <><p>上传要修改的原视频，并在提示词中说明如何修改。原视频会在提交任务后上传到所选账号的 Flow 项目。</p>{value.video && preview(value.video, 'video')}{picker('video', value.video ? '替换原视频' : '上传原视频')}<p>当前仅支持编辑原视频；不支持把视频作为动作参考与参考图混合生成。长度、比例和画质由原视频与 Flow 决定，每次 1 条。上游实际编辑尚待验证。</p></>}
    {error && <p className="material-error" role="alert">{error}</p>}
    {mode !== 'text' && <p className="material-local">上传这里只保存在本机；确认提交后才调用 Flow。移除只取消本次引用，不删除文件。</p>}
  </section>
}
