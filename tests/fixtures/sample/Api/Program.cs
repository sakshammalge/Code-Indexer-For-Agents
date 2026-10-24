using Api.Services;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddSingleton<IInventoryService, InventoryService>();
var app = builder.Build();
app.MapGet("/api/v1/items", (IInventoryService svc) => svc.GetAll());
app.Run();
