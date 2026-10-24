import React, { useEffect, useState } from "react";
import { fetchItems, InventoryItem } from "../api";

type Props = { title: string };

export default function ItemList({ title }: Props) {
  const [items, setItems] = useState<InventoryItem[]>([]);
  useEffect(() => { fetchItems().then(setItems); }, []);
  return (
    <ul className="item-list">
      {items.map((i) => <li key={i.sku}>{i.sku}</li>)}
    </ul>
  );
}

export const ItemBadge = ({ count }: { count: number }) => <span className="badge">{count}</span>;
