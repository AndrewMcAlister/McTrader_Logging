/* McTrader_Logging — the tables the logging service writes.
 *
 * These live in the dedicated `Logging` database. McTrader's own dbo.Logs stays in
 * McTrader_Live and is untouched by this project: it keeps its existing writers until each app is
 * migrated, so nothing has to be dropped or renamed to start using this service.
 *
 * McTrader's existing table (101,344 rows, columns
 * Id/Received/Location/Description/Log_Level_Id/Message/Broker_Id/Symbol/Order_Id) keeps
 * receiving its own writes until each app is migrated; a query can UNION the two during the
 * transition, and nothing has to be dropped or renamed to start using this service.
 *
 * What the new tables add, and why each one earns its column:
 *
 *   CorrelationId  the gap in dbo.Logs. Without it a single trading decision cannot be
 *                  followed across processes, and after the micro-service split one decision
 *                  spans Market_Data_Service -> trader -> broker in three processes. This is
 *                  the column the service exists for.
 *   CycleId        the trading cycle a row belongs to, so "everything that happened at
 *                  10:30" is a query rather than a timestamp range guess.
 *   Service        which app wrote it. dbo.Logs has Location but no tenant, so two apps
 *                  logging the same module name are indistinguishable.
 *   OccurredUtc    when the event happened, on the writer's clock, against ReceivedUtc which
 *                  is when this service stored it. The difference localises a backlog.
 *   Payload        structured detail as JSON, so a message does not have to be parsed.
 *
 * Clustered on the identity column: this is an append-only table and nothing ever updates a
 * row, so a narrow ever-increasing key is the right clustered index and a GUID would not be.
 */

SET NOCOUNT ON;

IF OBJECT_ID('dbo.LogEntry', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.LogEntry (
        LogId         bigint IDENTITY(1,1) NOT NULL,
        ReceivedUtc   datetime2(3)   NOT NULL CONSTRAINT DF_LogEntry_Received
                                     DEFAULT SYSUTCDATETIME(),
        OccurredUtc   datetime2(3)   NULL,
        Service       varchar(64)    NOT NULL,
        CorrelationId varchar(64)    NULL,
        CycleId       varchar(64)    NULL,
        Level         tinyint        NOT NULL,
        LevelName     varchar(16)    NOT NULL,
        Category      varchar(64)    NULL,
        Location      varchar(255)   NOT NULL,
        Message       nvarchar(2000) NULL,
        Symbol        varchar(50)    NULL,
        BrokerId      tinyint        NULL,
        OrderId       uniqueidentifier NULL,
        Payload       nvarchar(max)  NULL,
        CONSTRAINT PK_LogEntry PRIMARY KEY CLUSTERED (LogId),
        CONSTRAINT CK_LogEntry_Level CHECK (Level BETWEEN 1 AND 60)
    );

    /* "What happened recently", the commonest query. */
    CREATE NONCLUSTERED INDEX IX_LogEntry_Received
        ON dbo.LogEntry (ReceivedUtc DESC)
        INCLUDE (Service, Level, LevelName, Symbol, Message);

    /* "What happened in this decision" — the query that was impossible before. */
    CREATE NONCLUSTERED INDEX IX_LogEntry_Correlation
        ON dbo.LogEntry (CorrelationId, ReceivedUtc)
        WHERE CorrelationId IS NOT NULL;

    /* "What happened to this instrument". */
    CREATE NONCLUSTERED INDEX IX_LogEntry_Symbol
        ON dbo.LogEntry (Symbol, ReceivedUtc DESC)
        WHERE Symbol IS NOT NULL;
END
GO

IF OBJECT_ID('dbo.LogEvent', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.LogEvent (
        EventId       bigint IDENTITY(1,1) NOT NULL,
        ReceivedUtc   datetime2(3)   NOT NULL CONSTRAINT DF_LogEvent_Received
                                     DEFAULT SYSUTCDATETIME(),
        OccurredUtc   datetime2(3)   NULL,
        Service       varchar(64)    NOT NULL,
        CorrelationId varchar(64)    NULL,
        CycleId       varchar(64)    NULL,
        EventType     varchar(64)    NOT NULL,
        Symbol        varchar(50)    NULL,
        BrokerId      tinyint        NULL,
        OrderId       uniqueidentifier NULL,
        Payload       nvarchar(max)  NULL,
        CONSTRAINT PK_LogEvent PRIMARY KEY CLUSTERED (EventId)
    );

    CREATE NONCLUSTERED INDEX IX_LogEvent_Type
        ON dbo.LogEvent (EventType, ReceivedUtc DESC);
    CREATE NONCLUSTERED INDEX IX_LogEvent_Correlation
        ON dbo.LogEvent (CorrelationId, ReceivedUtc)
        WHERE CorrelationId IS NOT NULL;
END
GO
/* ------------------------------------------------------------------ */
/* Migration: OrderId is varchar, not uniqueidentifier                 */
/* ------------------------------------------------------------------ */
/* The first version declared OrderId uniqueidentifier, copied from McTrader's dbo.Logs. That
 * is wrong for a log sink: a broker order id is not guaranteed to be a UUID (Alpaca's are,
 * IBKR's are not, and a test or a replay uses something readable), and when a value does not
 * convert, SQL Server fails the whole INSERT -- so one non-UUID order id cost an entire batch
 * of otherwise good log lines. A field that can silently discard logs is the wrong type for a
 * column whose job is to help find them. */
IF EXISTS (
    SELECT 1 FROM sys.columns c
    JOIN sys.types t ON t.user_type_id = c.user_type_id
    WHERE c.object_id = OBJECT_ID('dbo.LogEntry') AND c.name = 'OrderId'
      AND t.name = 'uniqueidentifier')
BEGIN
    ALTER TABLE dbo.LogEntry ALTER COLUMN OrderId varchar(64) NULL;
END
GO

IF EXISTS (
    SELECT 1 FROM sys.columns c
    JOIN sys.types t ON t.user_type_id = c.user_type_id
    WHERE c.object_id = OBJECT_ID('dbo.LogEvent') AND c.name = 'OrderId'
      AND t.name = 'uniqueidentifier')
BEGIN
    ALTER TABLE dbo.LogEvent ALTER COLUMN OrderId varchar(64) NULL;
END
GO
/* ------------------------------------------------------------------ */
/* Migration: index the client name                                     */
/* ------------------------------------------------------------------ */
/* Service is the column that answers "which client wrote this". Without an index, every
 * filter by client and the per-service breakdown in /stats is a scan of a table that only
 * grows -- and the breakdown runs on every /stats call. Nonclustered, ordered (Service,
 * ReceivedUtc DESC) so "this client's most recent lines" is one seek. */
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('dbo.LogEntry') AND name = 'IX_LogEntry_Service')
BEGIN
    CREATE NONCLUSTERED INDEX IX_LogEntry_Service
        ON dbo.LogEntry (Service, ReceivedUtc DESC)
        INCLUDE (Level, LevelName, Message);
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('dbo.LogEvent') AND name = 'IX_LogEvent_Service')
BEGIN
    CREATE NONCLUSTERED INDEX IX_LogEvent_Service
        ON dbo.LogEvent (Service, ReceivedUtc DESC)
        INCLUDE (EventType);
END
GO

/* ------------------------------------------------------------------ */
/* LogLevels: the catalogue of levels, as data                         */
/* ------------------------------------------------------------------ */
/* Levels were an enum in the code, which meant adding one was a release. As a table it is an
 * INSERT, and the service loads it at startup.
 *
 * There is deliberately NO foreign key from LogEntry.Level or LogEvent.Level to this table.
 * The catalogue still carries the five built-ins in code as a fallback, so a write never fails
 * because the table is unreadable -- and an FK would reintroduce exactly that failure: delete a
 * level row, or read a catalogue that disagrees with the writer, and inserts start being refused.
 * A log sink that can silently discard a log line is not a log sink. The values are normalised in
 * code before insert instead, so they are consistent without being fragile.
 *
 * Aliases are a comma-separated list because the alias set is a property of the level, not a
 * separate entity: 'warn' has no meaning apart from WARNING.
 */
IF OBJECT_ID('dbo.LogLevels', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.LogLevels (
        Level       tinyint      NOT NULL,
        LevelName   varchar(16)  NOT NULL,
        Description varchar(255) NULL,
        Aliases     varchar(255) NULL,
        CONSTRAINT PK_LogLevels PRIMARY KEY CLUSTERED (Level),
        CONSTRAINT UQ_LogLevels_LevelName UNIQUE (LevelName),
        CONSTRAINT CK_LogLevels_Range CHECK (Level BETWEEN 1 AND 60)
    );
END
GO

/* Seeded with WHEN NOT MATCHED only, never updated: a deployment may reword a description or add
 * an alias, and a service restart must not quietly revert that. */
MERGE dbo.LogLevels AS target
USING (VALUES
    (10, 'DEBUG',    'Diagnostic detail; off in normal running.',  'debug,trace,verbose'),
    (20, 'INFO',     'Something happened as expected.',            'info,information,notice'),
    (30, 'WARNING',  'Something unexpected; the work carried on.', 'warn,warning'),
    (40, 'ERROR',    'An operation failed.',                       'error,err,exception'),
    (50, 'CRITICAL', 'The process, or a book, cannot continue.',   'critical,fatal,panic')
) AS source (Level, LevelName, Description, Aliases)
ON target.Level = source.Level
WHEN NOT MATCHED BY TARGET THEN
    INSERT (Level, LevelName, Description, Aliases)
    VALUES (source.Level, source.LevelName, source.Description, source.Aliases);
GO

/* ------------------------------------------------------------------ */
/* Migration: every row carries a level, events included               */
/* ------------------------------------------------------------------ */
/* Events were previously level-less: they recorded what happened, not how bad it was. That made
 * "everything at WARNING or above, including the events" impossible as a single query, and forced
 * a caller to look in two places with two different notions of severity. WITH VALUES backfills
 * the rows that already exist instead of failing on them. */
IF COL_LENGTH('dbo.LogEvent', 'Level') IS NULL
    ALTER TABLE dbo.LogEvent ADD Level tinyint NOT NULL
        CONSTRAINT DF_LogEvent_Level DEFAULT 20 WITH VALUES;
GO

IF COL_LENGTH('dbo.LogEvent', 'LevelName') IS NULL
    ALTER TABLE dbo.LogEvent ADD LevelName varchar(16) NOT NULL
        CONSTRAINT DF_LogEvent_LevelName DEFAULT 'INFO' WITH VALUES;
GO

/* The severity filter applied to events, alongside the existing per-service index. */
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('dbo.LogEvent') AND name = 'IX_LogEvent_Level')
BEGIN
    CREATE NONCLUSTERED INDEX IX_LogEvent_Level
        ON dbo.LogEvent (Level, ReceivedUtc DESC)
        INCLUDE (Service, EventType);
END
GO

/* ------------------------------------------------------------------ */
/* ServiceLogLevel: how much of each client's output to record         */
/* ------------------------------------------------------------------ */
/* This is a WRITE-side filter: a row is discarded before it reaches the table, which is the point
 * -- it saves the INSERT, not just the read. That makes it the most dangerous control in this
 * database, because a discarded row does not exist afterwards. Three things follow, and all three
 * are deliberate:
 *
 *   It applies to LogEntry ONLY, never to LogEvent.
 *
 *     Events are the record of what was done, not diagnostics. `order.submitted` is an INFO event,
 *     so a client set to ERROR would stop recording its own orders -- deleting the audit trail this
 *     service exists to keep, in the name of reducing noise. Noise is what DEBUG is for.
 *
 *   There is NO row unless someone deliberately set a threshold.
 *
 *     An absent row means "record everything". A default of INFO would silently start discarding
 *     every DEBUG line the day this migration ran, which is not something a migration should decide.
 *
 *   UpdatedBy/Reason/UpdatedUtc exist because changing this is an auditable act.
 *
 *     "Why did the log go quiet on the 29th" is a question that needs an answer in the data.
 */
IF OBJECT_ID('dbo.ServiceLogLevel', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ServiceLogLevel (
        Service    varchar(64)  NOT NULL,
        MinLevel   tinyint      NOT NULL,
        Reason     varchar(255) NULL,
        UpdatedBy  varchar(128) NULL,
        UpdatedUtc datetime2(3) NOT NULL CONSTRAINT DF_ServiceLogLevel_Updated
                                DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_ServiceLogLevel PRIMARY KEY CLUSTERED (Service),
        CONSTRAINT CK_ServiceLogLevel_Range CHECK (MinLevel BETWEEN 1 AND 60)
    );
END
GO
