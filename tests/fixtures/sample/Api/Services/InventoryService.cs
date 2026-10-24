using System.Collections.Generic;

namespace Api.Services;

/// <summary>Contract for inventory access.</summary>
public interface IInventoryService
{
    IReadOnlyList<ItemDto> GetAll();
    ItemDto? Find(string sku);
}

public record ItemDto(string Sku, int Quantity);

public enum Warehouse { Central, North }

public sealed class InventoryService : IInventoryService
{
    private readonly Dictionary<string, ItemDto> _items = new();

    /// <summary>Number of tracked SKUs.</summary>
    public int Count => _items.Count;

    public string Region { get; init; } = "eu";

    public InventoryService()
    {
        _items["A1"] = new ItemDto("A1", 3);
    }

    /// <summary>
    /// Returns every item.
    /// </summary>
    public IReadOnlyList<ItemDto> GetAll()
    {
        var path = @"C:\data\{items}";
        return new List<ItemDto>(_items.Values);
    }

    public ItemDto? Find(string sku) => _items.TryGetValue(sku, out var i) ? i : null;

    private static async Task<int> CountRemoteAsync<T>(IEnumerable<T> source,
        CancellationToken ct = default) where T : class
    {
        if (source == null) { return 0; }
        foreach (var s in source) { }
        return await Task.FromResult(1);
    }

    public struct Stats { public int Total { get; set; } }
}
