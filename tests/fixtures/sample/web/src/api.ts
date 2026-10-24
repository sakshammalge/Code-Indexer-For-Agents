/**
 * HTTP client for the inventory API.
 */
export const API_BASE = "/api/v1";

export interface InventoryItem {
  sku: string;
  quantity: number;
}

export type Sku = string;

export enum StockLevel { Low, Medium, High }

/** Fetch all items from the backend. */
export async function fetchItems(signal?: AbortSignal): Promise<InventoryItem[]> {
  const res = await fetch(`${API_BASE}/items`, { signal });
  return res.json();
}

export class InventoryClient {
  private cache = new Map<string, InventoryItem>();

  constructor(private readonly baseUrl: string) {}

  /** Look up a single item by SKU. */
  async getItem(sku: Sku): Promise<InventoryItem | undefined> {
    if (this.cache.has(sku)) { return this.cache.get(sku); }
    return undefined;
  }

  clear = () => { this.cache.clear(); };
}

export const formatSku = (sku: Sku): string => sku.toUpperCase();
