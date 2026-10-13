-- Table shapes for the sample database. Rebuilt from scratch by corpus/build.py.
-- The database default collation is case-insensitive; ref.Product.Sku is
-- deliberately case-sensitive so that per-column collation matters.

CREATE SCHEMA ref;
GO
CREATE SCHEMA sales;
GO
CREATE SCHEMA stg;
GO
CREATE SCHEMA audit;
GO

CREATE TABLE ref.Customer
(
    CustomerId    int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Customer PRIMARY KEY,
    CustomerCode  varchar(10)        NOT NULL CONSTRAINT UQ_Customer_Code UNIQUE,
    Name          nvarchar(100)      NOT NULL,
    Email         varchar(100)       NULL,
    Region        char(5)            NOT NULL,
    CreditLimit   money              NOT NULL CONSTRAINT DF_Customer_CreditLimit DEFAULT (0),
    IsActive      bit                NOT NULL CONSTRAINT DF_Customer_IsActive DEFAULT (1),
    CreatedAt     datetime           NOT NULL CONSTRAINT DF_Customer_CreatedAt DEFAULT (GETDATE()),
    DeletedAt     datetime           NULL
);
GO

CREATE TABLE ref.Product
(
    ProductId      int IDENTITY(100, 10) NOT NULL CONSTRAINT PK_Product PRIMARY KEY,
    Sku            varchar(20) COLLATE Latin1_General_CS_AS NOT NULL CONSTRAINT UQ_Product_Sku UNIQUE,
    Name           nvarchar(100)  NOT NULL,
    Category       varchar(30)    NOT NULL,
    UnitPrice      money          NOT NULL,
    WeightKg       decimal(9, 3)  NULL,
    DiscontinuedOn date           NULL,
    CONSTRAINT CK_Product_UnitPrice CHECK (UnitPrice >= 0)
);
GO

CREATE TABLE sales.Inventory
(
    ProductId    int NOT NULL CONSTRAINT PK_Inventory PRIMARY KEY
                     CONSTRAINT FK_Inventory_Product REFERENCES ref.Product (ProductId),
    OnHand       int NOT NULL,
    ReorderLevel int NOT NULL,
    CONSTRAINT CK_Inventory_OnHand CHECK (OnHand >= 0)
);
GO

CREATE TABLE sales.[Order]
(
    OrderId     int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Order PRIMARY KEY,
    CustomerId  int           NOT NULL CONSTRAINT FK_Order_Customer REFERENCES ref.Customer (CustomerId),
    OrderDate   datetime      NOT NULL,
    ShippedAt   datetime2(7)  NULL,
    Status      tinyint       NOT NULL CONSTRAINT DF_Order_Status DEFAULT (0), -- 0 new, 1 shipped, 2 closed, 9 cancelled
    Note        nvarchar(200) NULL,
    TotalAmount money         NOT NULL CONSTRAINT DF_Order_TotalAmount DEFAULT (0)
);
GO

CREATE TABLE sales.OrderLine
(
    OrderLineId int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_OrderLine PRIMARY KEY,
    OrderId     int     NOT NULL CONSTRAINT FK_OrderLine_Order REFERENCES sales.[Order] (OrderId),
    ProductId   int     NOT NULL CONSTRAINT FK_OrderLine_Product REFERENCES ref.Product (ProductId),
    Quantity    int     NOT NULL,
    UnitPrice   money   NOT NULL,
    DiscountPct tinyint NOT NULL CONSTRAINT DF_OrderLine_DiscountPct DEFAULT (0),
    CONSTRAINT CK_OrderLine_Quantity CHECK (Quantity > 0)
);
GO

CREATE TABLE sales.OrderArchive
(
    OrderId     int           NOT NULL CONSTRAINT PK_OrderArchive PRIMARY KEY,
    CustomerId  int           NOT NULL,
    OrderDate   datetime      NOT NULL,
    Status      tinyint       NOT NULL,
    TotalAmount money         NOT NULL,
    ArchivedAt  datetime      NOT NULL
);
GO

CREATE TABLE sales.DailySales
(
    SalesDate date  NOT NULL,
    ProductId int   NOT NULL,
    Units     int   NOT NULL,
    Revenue   money NOT NULL,
    CONSTRAINT PK_DailySales PRIMARY KEY (SalesDate, ProductId)
);
GO

CREATE TABLE stg.CustomerImport
(
    CustomerCode varchar(10)   NULL,
    Name         nvarchar(100) NULL,
    Email        varchar(100)  NULL,
    Region       varchar(10)   NULL,
    CreditLimit  varchar(20)   NULL,
    LoadedAt     datetime      NOT NULL CONSTRAINT DF_CustomerImport_LoadedAt DEFAULT (GETDATE())
);
GO

CREATE TABLE stg.ProductPriceImport
(
    Sku       varchar(20) NOT NULL,
    UnitPrice money       NOT NULL,
    Category  varchar(30) NULL
);
GO

CREATE TABLE audit.OrderAudit
(
    AuditId   int IDENTITY(1000, 1) NOT NULL CONSTRAINT PK_OrderAudit PRIMARY KEY,
    OrderId   int      NOT NULL,
    Action    char(1)  NOT NULL,
    OldStatus tinyint  NULL,
    NewStatus tinyint  NULL,
    ChangedAt datetime NOT NULL CONSTRAINT DF_OrderAudit_ChangedAt DEFAULT (GETDATE()),
    ChangedBy sysname  NOT NULL CONSTRAINT DF_OrderAudit_ChangedBy DEFAULT (SUSER_SNAME())
);
GO

CREATE TABLE audit.ErrorLog
(
    ErrorLogId   int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_ErrorLog PRIMARY KEY,
    ProcName     sysname        NOT NULL,
    ErrorNumber  int            NOT NULL,
    ErrorMessage nvarchar(4000) NOT NULL,
    LoggedAt     datetime       NOT NULL CONSTRAINT DF_ErrorLog_LoggedAt DEFAULT (GETDATE())
);
GO

CREATE TYPE sales.OrderLineInput AS TABLE
(
    Sku         varchar(20) NOT NULL,
    Quantity    int         NOT NULL,
    DiscountPct tinyint     NOT NULL DEFAULT (0)
);
GO
