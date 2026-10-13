-- SQL Server Agent job: nightly sales roll-up. A recognised artefact type:
-- the tool lists it as manual work and does not convert it.
-- Scripted the way SQL Server Management Studio scripts a job.

USE msdb;
GO

IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = N'RetailSample - Nightly sales roll-up')
    EXEC msdb.dbo.sp_delete_job @job_name = N'RetailSample - Nightly sales roll-up';
GO

DECLARE @jobId binary(16);

EXEC msdb.dbo.sp_add_job
     @job_name = N'RetailSample - Nightly sales roll-up',
     @enabled = 1,
     @description = N'Recalculates sales.DailySales for yesterday, then closes stale orders.',
     @category_name = N'[Uncategorized (Local)]',
     @job_id = @jobId OUTPUT;

EXEC msdb.dbo.sp_add_jobstep
     @job_id = @jobId,
     @step_name = N'Recalculate daily sales',
     @step_id = 1,
     @subsystem = N'TSQL',
     @database_name = N'RetailSample',
     @command = N'DECLARE @d date = DATEADD(day, -1, CAST(GETDATE() AS date));
EXEC sales.uspRecalculateDailySales @FromDate = @d, @ToDate = @d;',
     @on_success_action = 3,   -- go to the next step
     @on_fail_action = 2,      -- quit reporting failure
     @retry_attempts = 2,
     @retry_interval = 5;

EXEC msdb.dbo.sp_add_jobstep
     @job_id = @jobId,
     @step_name = N'Close stale orders',
     @step_id = 2,
     @subsystem = N'TSQL',
     @database_name = N'RetailSample',
     @command = N'DECLARE @now datetime = GETDATE();
EXEC sales.uspCloseStaleOrders @AsOf = @now, @StaleAfter = 30;',
     @on_success_action = 1,   -- quit reporting success
     @on_fail_action = 2;

EXEC msdb.dbo.sp_add_jobschedule
     @job_id = @jobId,
     @name = N'Every night at 01:30',
     @freq_type = 4,           -- daily
     @freq_interval = 1,
     @active_start_time = 013000;

EXEC msdb.dbo.sp_add_jobserver
     @job_id = @jobId,
     @server_name = N'(local)';
GO
