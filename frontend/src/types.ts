import type { Geometry } from 'geojson';
export type Source = 'sentinel2' | 'landsat' | 'sentinel1' | 'uav';
export type Mode = 'indices' | 'change' | 'timeseries' | 'flood';
export interface Scene { source: Source; date: string; end_date?: string; upload_id?: string; bands?: Record<string, number>; scale: number; offset: number; orbit_pass?: 'ASCENDING' | 'DESCENDING'; relative_orbit?: number }
export interface Recipe { name: string; formula: string; bands: string[] }
export interface Registry { schema_version: string; indices: Recipe[]; sources: Source[] }
export interface Request { schema_version: '1.0'; mode: Mode; roi: Geometry; resolution: number; indices: string[]; scenes: Scene[]; rules: Record<string, number> }
export interface Job { job_id: string; status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'; progress: number; error?: string | null }
export interface Statistic { scene: number; index: string; date: string; count: number; mean: number | null; median: number | null; std: number | null; min: number | null; max: number | null; p10: number | null; p90: number | null; valid_fraction: number }
export interface Artifact { name: string; url: string }
export interface Layer { name: string; scene: number; index: string; url: string; bounds: [[number,number],[number,number]]; kind?: string }
export interface Result { schema_version: string; statistics: Statistic[]; quality: unknown[]; artifacts: Artifact[]; layers?: Layer[]; [key: string]: unknown }
export interface Upload { upload_id: string; bands: number; crs: string; bounds: number[] }
