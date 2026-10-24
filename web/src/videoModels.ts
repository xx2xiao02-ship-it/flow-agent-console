export const videoModels = [
  { value: 'omni_flash', label: 'Omni 1.1 Flash' },
  { value: 'veo_3_1_lite', label: 'Veo 3.1 Lite' },
  { value: 'veo_3_1_fast', label: 'Veo 3.1 Fast' },
  { value: 'veo_3_1_quality', label: 'Veo 3.1 Quality' },
] as const
export type VideoModel = typeof videoModels[number]['value']
export const videoModelLabel = (value?: string) => videoModels.find(m => m.value === (value || 'omni_flash'))?.label || value || ''
export const supportsVideoMode = (model: string, mode: string) => model === 'omni_flash' || (mode !== 'edit' && !(model === 'veo_3_1_quality' && mode === 'references'))
