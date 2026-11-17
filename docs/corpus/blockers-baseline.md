# Corpus blockers: the baseline

What stops the 82 procedures and triggers of the three corpus databases from generating whole, measured by
[`tools/corpus_blockers.py`](../../tools/corpus_blockers.py) at commit `dbe2c0b` (after #117, before the work that follows it).
Every ticket report quotes the tool's output before and after; `engine/tests/test_corpus_blockers.py` pins the headline numbers.
This file is the starting point, so a later number can be read against it. It is not kept up to date: run the tool for today's.

Read the tables with the caveat below, and with these three facts:

- A construct blocks a procedure once however many statements hold it. The first failure of a statement hides the rest of it, so
  every kind with no generation beneath a failed statement is counted too.
- A statement the generator handles that is refused only because a scope barrier (dynamic SQL, a construct kept as written) made
  its scope uncertain is the barrier's cascade, and is not counted. The 153 such items of 5 procedures collapse to their barriers.
- 17 procedures hold dynamic SQL or a schema change, which stay manual work for good, so no more than 65 of the 82 can ever be whole.

Regenerate it with `python tools/corpus_blockers.py --full` in the sandbox toolbox (see the tool's docstring).

## The measurement

procedures and triggers measured: 82
root blockers remaining: 568
procedures with at most 1 left: 13; at most 3 left: 24
whole: 0 of 82 (ceiling 65: the rest hold dynamic SQL or a schema change)

caveat: lower bound. A statement reports only its first failure, so a gap hidden under it (a type, a collation, a deeper construct) shows up when the shallower ones are fixed, and the total can rise.

By waiting ticket

| ticket | procedures it stops | only remaining ticket for |
|---|---|---|
| none | 62 | 2 |
| #119 | 55 | 1 |
| #63 | 47 | 10 |
| #118 | 44 | 2 |
| #120 | 24 | 0 |
| #50 | 17 | 0 |
| #87 | 14 | 0 |
| #124 | 10 | 0 |
| #122 | 9 | 0 |
| #113 | 7 | 0 |
| #121 | 4 | 0 |

By kind of construct

| blocker kind | waiting ticket | procedures it stops | procedures it alone stops |
|---|---|---|---|
| opaque_fragment | #63 | 47 | 10 |
| set_abort_on_error | #119 | 31 | 0 |
| try_catch | #119 | 27 | 0 |
| call_procedure | #118 | 22 | 2 |
| update_rows | #118 | 19 | 0 |
| emit_message | #119 | 18 | 0 |
| rollback_transaction | #119 | 18 | 0 |
| sort | none | 18 | 1 |
| dynamic_sql_execution | none | 15 | 0 |
| insert_rows | #118 | 15 | 0 |
| concat_null_propagating | none | 13 | 0 |
| implicit_convert | #50, #87 | 13 | 0 |
| scalar_query | none | 13 | 0 |
| transaction_depth | #119 | 13 | 0 |
| begin_transaction | #119 | 12 | 0 |
| commit_transaction | #119 | 12 | 0 |
| read_values | none | 12 | 0 |
| raise_error | #119 | 11 | 0 |
| rethrow | #119 | 11 | 0 |
| close_cursor | #124 | 10 | 0 |
| convert | none | 10 | 0 |
| create_temp_table | #120 | 10 | 0 |
| current_time | none | 10 | 0 |
| deallocate_cursor | #124 | 10 | 0 |
| declare_cursor | #124 | 10 | 0 |
| fetch_cursor | #124 | 10 | 0 |
| fetch_status | #124 | 10 | 0 |
| first_non_null | none | 10 | 0 |
| open_cursor | #124 | 10 | 0 |
| assign_from_rows | #120 | 9 | 0 |
| drop_temp_table | #120 | 9 | 0 |
| negate | none | 9 | 0 |
| read_changed_rows | #122 | 9 | 0 |
| union | none | 9 | 0 |
| transaction_state | #113 | 7 | 0 |
| declare_row_set_variable | #120 | 6 | 0 |
| declare_parameter | none | 5 | 0 |
| error_field | #119 | 5 | 0 |
| delete_rows | #118 | 4 | 0 |
| named_query | none | 4 | 0 |
| trim | none | 4 | 0 |
| date_add | none | 3 | 0 |
| exists | none | 3 | 0 |
| in_list | none | 3 | 0 |
| last_identity | #50 | 3 | 0 |
| return_changed_rows | #121 | 3 | 0 |
| schema_change | none | 3 | 0 |
| between | none | 2 | 0 |
| decimal_divide | none | 2 | 0 |
| merge_clause | none | 2 | 0 |
| merge_rows | #121 | 2 | 0 |
| read_table | #120 | 2 | 0 |
| rollback_to_save_point | #119 | 2 | 0 |
| round | none | 2 | 0 |
| save_point | #119 | 2 | 0 |
| upper | none | 2 | 0 |
| window | none | 2 | 0 |
| changed_row_action | none | 1 | 0 |
| convert_with_style | none | 1 | 0 |
| date_diff_boundaries | none | 1 | 0 |
| declare_label | #50 | 1 | 0 |
| jump | #50 | 1 | 0 |
| left_part | none | 1 | 0 |
| limit | none | 1 | 0 |
| row_number | none | 1 | 0 |
| subtract | #87 | 1 | 0 |
| try_convert | none | 1 | 0 |

By procedure

| procedure | distinct blockers | blockers |
|---|---|---|
| AdventureWorks2022.Person.iuPerson | 1 | opaque_fragment |
| AdventureWorks2022.dbo.uspGetBillOfMaterials | 1 | opaque_fragment |
| AdventureWorks2022.dbo.uspGetEmployeeManagers | 1 | opaque_fragment |
| AdventureWorks2022.dbo.uspGetManagerEmployees | 1 | opaque_fragment |
| AdventureWorks2022.dbo.uspGetWhereUsedProductID | 1 | opaque_fragment |
| RetailSample.sales.uspGetOrdersByCustomer | 1 | sort |
| WideWorldImporters.Application.Configuration_ConfigureForEnterpriseEdition | 1 | call_procedure |
| WideWorldImporters.Sequences.ReseedAllSequences | 1 | call_procedure |
| WideWorldImporters.Website.SearchForCustomers | 1 | opaque_fragment |
| WideWorldImporters.Website.SearchForPeople | 1 | opaque_fragment |
| WideWorldImporters.Website.SearchForStockItems | 1 | opaque_fragment |
| WideWorldImporters.Website.SearchForStockItemsByTags | 1 | opaque_fragment |
| WideWorldImporters.Website.SearchForSuppliers | 1 | opaque_fragment |
| RetailSample.sales.uspSearchOrders | 2 | concat_null_propagating, dynamic_sql_execution |
| WideWorldImporters.DataLoadSimulation.Configuration_RemoveDataLoadSimulationProcedures | 2 | schema_change, set_abort_on_error |
| WideWorldImporters.Integration.GetStockHoldingUpdates | 2 | set_abort_on_error, sort |
| WideWorldImporters.Website.RecordColdRoomTemperatures | 2 | declare_parameter, opaque_fragment |
| AdventureWorks2022.HumanResources.dEmployee | 3 | raise_error, rollback_transaction, transaction_depth |
| AdventureWorks2022.HumanResources.uspUpdateEmployeePersonalInfo | 3 | call_procedure, try_catch, update_rows |
| WideWorldImporters.Application.Configuration_ApplyFullTextIndexing | 3 | dynamic_sql_execution, emit_message, opaque_fragment |
| WideWorldImporters.DataLoadSimulation.ReactivateTemporalTablesAfterDataLoad | 3 | call_procedure, opaque_fragment, schema_change |
| WideWorldImporters.Integration.GetMovementUpdates | 3 | convert, set_abort_on_error, sort |
| WideWorldImporters.Integration.GetPurchaseUpdates | 3 | convert, set_abort_on_error, sort |
| WideWorldImporters.Integration.GetSaleUpdates | 3 | convert, set_abort_on_error, sort |
| AdventureWorks2022.HumanResources.uspUpdateEmployeeLogin | 4 | call_procedure, declare_parameter, try_catch, update_rows |
| AdventureWorks2022.dbo.uspPrintError | 4 | concat_null_propagating, convert, emit_message, error_field |
| RetailSample.sales.trOrderLine_MaintainTotal | 4 | named_query, read_changed_rows, union, update_rows |
| RetailSample.sales.trOrder_AuditStatus | 4 | exists, insert_rows, opaque_fragment, read_changed_rows |
| WideWorldImporters.DataLoadSimulation.Configuration_ApplyDataLoadSimulationProcedures | 4 | call_procedure, dynamic_sql_execution, opaque_fragment, set_abort_on_error |
| WideWorldImporters.DataLoadSimulation.DeactivateTemporalTablesBeforeDataLoad | 4 | call_procedure, dynamic_sql_execution, opaque_fragment, schema_change |
| WideWorldImporters.Integration.GetTransactionUpdates | 4 | convert, first_non_null, set_abort_on_error, union |
| AdventureWorks2022.Purchasing.iPurchaseOrderDetail | 5 | call_procedure, opaque_fragment, rollback_transaction, transaction_depth, try_catch |
| AdventureWorks2022.Purchasing.uPurchaseOrderDetail | 5 | call_procedure, opaque_fragment, rollback_transaction, transaction_depth, try_catch |
| AdventureWorks2022.Purchasing.uPurchaseOrderHeader | 5 | call_procedure, opaque_fragment, rollback_transaction, transaction_depth, try_catch |
| AdventureWorks2022.Sales.iduSalesOrderDetail | 5 | call_procedure, opaque_fragment, rollback_transaction, transaction_depth, try_catch |
| AdventureWorks2022.Sales.uSalesOrderHeader | 5 | call_procedure, opaque_fragment, rollback_transaction, transaction_depth, try_catch |
| AdventureWorks2022.dbo.uspSearchCandidateResumes | 5 | assign_from_rows, concat_null_propagating, implicit_convert, opaque_fragment, read_values |
| RetailSample.ref.trCustomer_SoftDelete | 5 | current_time, delete_rows, exists, read_changed_rows, update_rows |
| WideWorldImporters.Application.Configuration_RemoveRowLevelSecurity | 5 | dynamic_sql_execution, emit_message, raise_error, set_abort_on_error, try_catch |
| WideWorldImporters.Integration.GetOrderUpdates | 5 | convert, decimal_divide, round, set_abort_on_error, sort |
| WideWorldImporters.Sequences.ReseedSequenceBeyondTableValues | 5 | create_temp_table, drop_temp_table, dynamic_sql_execution, opaque_fragment, scalar_query |
| WideWorldImporters.Website.ChangePassword | 5 | emit_message, negate, opaque_fragment, raise_error, set_abort_on_error |
| RetailSample.audit.uspLogError | 6 | error_field, insert_rows, negate, read_values, rollback_transaction, transaction_state |
| WideWorldImporters.Application.Configuration_ApplyPartitioning | 6 | begin_transaction, commit_transaction, dynamic_sql_execution, emit_message, opaque_fragment, set_abort_on_error |
| WideWorldImporters.Application.Configuration_ApplyRowLevelSecurity | 6 | dynamic_sql_execution, emit_message, error_field, raise_error, set_abort_on_error, try_catch |
| AdventureWorks2022.Production.iWorkOrder | 7 | call_procedure, current_time, insert_rows, read_changed_rows, rollback_transaction, transaction_depth, try_catch |
| AdventureWorks2022.Purchasing.dVendor | 7 | assign_from_rows, call_procedure, raise_error, read_changed_rows, rollback_transaction, transaction_depth, try_catch |
| WideWorldImporters.Application.AddRoleMemberIfNonexistent | 7 | concat_null_propagating, dynamic_sql_execution, emit_message, opaque_fragment, rethrow, set_abort_on_error, try_catch |
| WideWorldImporters.Application.Configuration_ApplyAuditing | 7 | dynamic_sql_execution, emit_message, implicit_convert, opaque_fragment, rethrow, set_abort_on_error, try_catch |
| WideWorldImporters.Application.Configuration_EnableInMemory | 7 | concat_null_propagating, dynamic_sql_execution, emit_message, opaque_fragment, rethrow, set_abort_on_error, try_catch |
| WideWorldImporters.Application.Configuration_RemoveAuditing | 7 | dynamic_sql_execution, emit_message, implicit_convert, opaque_fragment, rethrow, set_abort_on_error, try_catch |
| WideWorldImporters.Application.CreateRoleIfNonexistent | 7 | concat_null_propagating, dynamic_sql_execution, emit_message, opaque_fragment, rethrow, set_abort_on_error, try_catch |
| WideWorldImporters.Website.ActivateWebsiteLogon | 7 | emit_message, negate, opaque_fragment, raise_error, scalar_query, set_abort_on_error, update_rows |
| AdventureWorks2022.Production.uWorkOrder | 8 | call_procedure, current_time, insert_rows, opaque_fragment, read_changed_rows, rollback_transaction, transaction_depth, try_catch |
| RetailSample.sales.uspTransferStock | 8 | assign_from_rows, begin_transaction, commit_transaction, insert_rows, negate, read_values, rollback_transaction, update_rows |
| RetailSample.sales.uspUpdateOrderStatus | 8 | current_time, declare_row_set_variable, in_list, raise_error, read_changed_rows, read_table, return_changed_rows, update_rows |
| WideWorldImporters.DataLoadSimulation.PopulateDataToCurrentDate | 8 | call_procedure, convert, current_time, date_add, first_non_null, negate, opaque_fragment, scalar_query |
| AdventureWorks2022.HumanResources.uspUpdateEmployeeHireInfo | 9 | begin_transaction, call_procedure, commit_transaction, insert_rows, read_values, rollback_transaction, transaction_depth, try_catch, update_rows |
| AdventureWorks2022.dbo.uspLogError | 9 | call_procedure, concat_null_propagating, emit_message, error_field, last_identity, negate, opaque_fragment, transaction_state, try_catch |
| RetailSample.stg.uspLoadCustomerBatch | 9 | assign_from_rows, begin_transaction, commit_transaction, declare_label, insert_rows, jump, read_values, rollback_transaction, set_abort_on_error |
| RetailSample.stg.uspPurgeStaging | 9 | close_cursor, deallocate_cursor, declare_cursor, dynamic_sql_execution, fetch_cursor, fetch_status, implicit_convert, opaque_fragment, open_cursor |
| WideWorldImporters.Application.Configuration_ApplyColumnstoreIndexing | 9 | begin_transaction, commit_transaction, concat_null_propagating, dynamic_sql_execution, emit_message, opaque_fragment, rethrow, set_abort_on_error, try_catch |
| RetailSample.ref.uspUpsertCustomer | 10 | assign_from_rows, call_procedure, error_field, in_list, insert_rows, last_identity, read_values, rethrow, try_catch, update_rows |
| RetailSample.sales.uspRecalculateDailySales | 10 | begin_transaction, between, commit_transaction, convert, create_temp_table, date_add, delete_rows, drop_temp_table, insert_rows, read_values |
| RetailSample.sales.uspReorderReport | 10 | close_cursor, create_temp_table, deallocate_cursor, declare_cursor, fetch_cursor, fetch_status, insert_rows, open_cursor, read_values, sort |
| WideWorldImporters.Website.RecordVehicleTemperature | 10 | begin_transaction, commit_transaction, concat_null_propagating, emit_message, opaque_fragment, raise_error, rollback_transaction, set_abort_on_error, transaction_state, try_catch |
| RetailSample.ref.uspMergeCustomerImport | 11 | assign_from_rows, delete_rows, merge_clause, merge_rows, named_query, read_values, row_number, trim, try_convert, upper, window |
| RetailSample.sales.uspApplyPriceImport | 14 | changed_row_action, concat_null_propagating, convert, current_time, declare_row_set_variable, implicit_convert, merge_clause, merge_rows, named_query, read_changed_rows, read_table, return_changed_rows, trim, upper |
| RetailSample.sales.uspCustomerStatement | 14 | assign_from_rows, between, convert_with_style, date_add, declare_row_set_variable, insert_rows, negate, opaque_fragment, raise_error, scalar_query, sort, subtract, trim, window |
| WideWorldImporters.Website.InsertCustomerOrders | 14 | begin_transaction, commit_transaction, current_time, declare_parameter, declare_row_set_variable, emit_message, insert_rows, negate, opaque_fragment, rethrow, rollback_transaction, set_abort_on_error, transaction_state, try_catch |
| RetailSample.sales.uspCancelOrder | 15 | begin_transaction, call_procedure, commit_transaction, concat_null_propagating, left_part, named_query, rethrow, rollback_to_save_point, rollback_transaction, save_point, transaction_depth, transaction_state, trim, try_catch, update_rows |
| RetailSample.sales.uspCloseStaleOrders | 16 | assign_from_rows, close_cursor, concat_null_propagating, convert, date_diff_boundaries, deallocate_cursor, declare_cursor, emit_message, fetch_cursor, fetch_status, implicit_convert, in_list, open_cursor, read_values, sort, update_rows |
| WideWorldImporters.Integration.GetCityUpdates | 16 | close_cursor, create_temp_table, deallocate_cursor, declare_cursor, drop_temp_table, fetch_cursor, fetch_status, first_non_null, implicit_convert, opaque_fragment, open_cursor, scalar_query, set_abort_on_error, sort, union, update_rows |
| WideWorldImporters.Integration.GetCustomerUpdates | 16 | close_cursor, create_temp_table, deallocate_cursor, declare_cursor, drop_temp_table, fetch_cursor, fetch_status, first_non_null, implicit_convert, opaque_fragment, open_cursor, scalar_query, set_abort_on_error, sort, union, update_rows |
| WideWorldImporters.Integration.GetEmployeeUpdates | 16 | close_cursor, create_temp_table, deallocate_cursor, declare_cursor, drop_temp_table, fetch_cursor, fetch_status, first_non_null, implicit_convert, opaque_fragment, open_cursor, scalar_query, set_abort_on_error, sort, union, update_rows |
| WideWorldImporters.Integration.GetPaymentMethodUpdates | 16 | close_cursor, create_temp_table, deallocate_cursor, declare_cursor, drop_temp_table, fetch_cursor, fetch_status, first_non_null, implicit_convert, opaque_fragment, open_cursor, scalar_query, set_abort_on_error, sort, union, update_rows |
| WideWorldImporters.Integration.GetStockItemUpdates | 16 | close_cursor, create_temp_table, deallocate_cursor, declare_cursor, drop_temp_table, fetch_cursor, fetch_status, first_non_null, implicit_convert, opaque_fragment, open_cursor, scalar_query, set_abort_on_error, sort, union, update_rows |
| WideWorldImporters.Integration.GetSupplierUpdates | 16 | close_cursor, create_temp_table, deallocate_cursor, declare_cursor, drop_temp_table, fetch_cursor, fetch_status, first_non_null, implicit_convert, opaque_fragment, open_cursor, scalar_query, set_abort_on_error, sort, union, update_rows |
| WideWorldImporters.Integration.GetTransactionTypeUpdates | 16 | close_cursor, create_temp_table, deallocate_cursor, declare_cursor, drop_temp_table, fetch_cursor, fetch_status, first_non_null, implicit_convert, opaque_fragment, open_cursor, scalar_query, set_abort_on_error, sort, union, update_rows |
| RetailSample.sales.uspArchiveOrders | 17 | begin_transaction, call_procedure, commit_transaction, current_time, declare_row_set_variable, delete_rows, insert_rows, limit, read_changed_rows, read_values, return_changed_rows, rollback_to_save_point, save_point, sort, transaction_depth, transaction_state, try_catch |
| RetailSample.sales.uspCreateOrder | 17 | assign_from_rows, begin_transaction, call_procedure, commit_transaction, current_time, declare_parameter, insert_rows, last_identity, raise_error, read_values, rethrow, rollback_transaction, scalar_query, set_abort_on_error, transaction_depth, try_catch, update_rows |
| WideWorldImporters.Website.InvoiceCustomerOrders | 22 | begin_transaction, commit_transaction, concat_null_propagating, current_time, decimal_divide, declare_parameter, declare_row_set_variable, emit_message, exists, first_non_null, insert_rows, negate, opaque_fragment, raise_error, rethrow, rollback_transaction, round, scalar_query, set_abort_on_error, sort, transaction_state, try_catch |
