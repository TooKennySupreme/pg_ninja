CREATE OR REPLACE FUNCTION sch_ninja.fn_process_batch(integer,integer)
RETURNS BOOLEAN AS
$BODY$
	DECLARE
		p_i_max_events	ALIAS FOR $1;
		p_i_source_id		ALIAS FOR $2;
		v_b_loop		boolean;
		v_r_rows		record;
		v_i_id_batch		bigint;
		v_t_ddl		text;
		v_i_replayed		integer;
		v_i_skipped		integer;
		v_i_ddl		integer;
	BEGIN
		v_b_loop:=FALSE;
		v_i_replayed:=0;
		v_i_ddl:=0;
		v_i_skipped:=0;
		
		v_i_id_batch:= (
			SELECT 
				i_id_batch 
			FROM ONLY
				sch_ninja.t_replica_batch  
			WHERE 
					b_started 
				AND	b_processed 
				AND	NOT b_replayed
				AND	i_id_source=p_i_source_id
			ORDER BY 
				ts_created 
			LIMIT 1
			)
		;
		IF v_i_id_batch IS NULL 
		THEN
			RETURN v_b_loop;
		END IF;
		RAISE DEBUG 'Found id_batch %', v_i_id_batch;
		
		FOR v_r_rows IN 
			SELECT 
				CASE
					WHEN enm_binlog_event = 'ddl'
					THEN 
						t_query
					WHEN enm_binlog_event = 'insert'
					THEN
						format(
							'INSERT INTO %I.%I (%s) VALUES (%s)  ON CONFLICT DO NOTHING;',
							v_schema_name,
							v_table_name,
							array_to_string(t_colunm,','),
							array_to_string(t_event_data,',')
							
						)
					WHEN enm_binlog_event = 'update'
					THEN
						format(
							'UPDATE %I.%I SET %s WHERE %s;',
							v_schema_name,
							v_table_name,
							t_update,
							t_pk_update
						)
					WHEN enm_binlog_event = 'delete'
					THEN
						format(
							'DELETE FROM %I.%I WHERE %s;',
							v_schema_name,
							v_table_name,
							t_pk_data
						)
					
				END AS t_sql,
				i_id_event,
				i_id_batch,
				enm_binlog_event
			FROM
			(
				SELECT
					i_id_event,
					i_id_batch,
					v_table_name,
					v_schema_name,
					enm_binlog_event,
					t_query,
					ts_event_datetime,
					t_pk_data,
					t_pk_update,
					array_agg(quote_ident(t_column)) AS t_colunm,
					string_agg(distinct format('%I=%L',t_column,jsb_event_data->>t_column),',') as  t_update,
					array_agg(quote_literal(jsb_event_data->>t_column)) as t_event_data
				FROM
				(
					SELECT
						i_id_event,
						i_id_batch,
						v_table_name,
						v_schema_name,
						enm_binlog_event,
						jsb_event_data,
						jsb_event_update,
						t_query,
						ts_event_datetime,
						string_agg(distinct format('%I=%L',v_pkey,jsb_event_data->>v_pkey),' AND ') as  t_pk_data,
						string_agg(distinct format('%I=%L',v_pkey,jsb_event_update->>v_pkey),' AND ') as  t_pk_update,
						(jsonb_each_text(coalesce(jsb_event_data,'{"foo":"bar"}'::jsonb))).key AS t_column
					FROM
					(
						SELECT 
							i_id_event,
							i_id_batch,
							v_table_name,
							v_schema_name,
							enm_binlog_event,
							jsb_event_data,
							jsb_event_update,
							t_query,
							ts_event_datetime,
							replace(unnest(string_to_array(v_table_pkey[1],',')),'"','') as v_pkey
							
							
							
						FROM 
							(
								SELECT 
									log.i_id_event,
									log.i_id_batch,
									log.v_table_name,
									log.v_schema_name,
									log.enm_binlog_event,
									log.jsb_event_data,
									log.jsb_event_update,
									log.t_query,
									ts_event_datetime,
									v_table_pkey
									
									
									
								FROM 
									sch_ninja.t_log_replica  log
									INNER JOIN sch_ninja.t_replica_tables tab
										ON
												tab.v_table_name=log.v_table_name
											AND tab.v_schema_name=log.v_schema_name
								WHERE
									log.i_id_batch=v_i_id_batch
								LIMIT p_i_max_events
							) t_log
							
					) t_pkey
					GROUP BY
						i_id_event,
						i_id_batch,
						v_table_name,
						v_schema_name,
						enm_binlog_event,
						jsb_event_data,
						jsb_event_update,
						t_query,
						ts_event_datetime
				) t_columns
				GROUP BY
					i_id_event,
					i_id_batch,
					v_table_name,
					v_schema_name,
					enm_binlog_event,
					t_query,
					ts_event_datetime,
					t_pk_data,
					t_pk_update
			) t_sql
		LOOP 	
			EXECUTE  v_r_rows.t_sql;
			IF v_r_rows.enm_binlog_event='ddl'
			THEN
				v_i_ddl:=v_i_ddl+1;
			ELSE
				v_i_replayed:=v_i_replayed+1;
			END IF;
			
			
			DELETE FROM sch_ninja.t_log_replica
			WHERE
				i_id_event=v_r_rows.i_id_event
			;
			
		END LOOP;
		

		IF v_i_replayed=0 AND v_i_ddl=0
		THEN
			DELETE FROM sch_ninja.t_log_replica
			WHERE
    			    i_id_batch=v_i_id_batch
			;
				
			GET DIAGNOSTICS v_i_skipped = ROW_COUNT;

			UPDATE ONLY sch_ninja.t_replica_batch  
			SET 
				b_replayed=True,
				i_skipped=v_i_skipped,
				ts_replayed=clock_timestamp()
				
			WHERE
				i_id_batch=v_i_id_batch
			;

			

			v_b_loop=False;
		ELSE
			UPDATE ONLY sch_ninja.t_replica_batch  
			SET 
				i_ddl=coalesce(i_ddl,0)+v_i_ddl,
				i_replayed=coalesce(i_replayed,0)+v_i_replayed,
				ts_replayed=clock_timestamp()
			WHERE
				i_id_batch=v_r_rows.i_id_batch
			;
			v_b_loop=True;
		END IF;

		RETURN v_b_loop;

	
	END;
$BODY$
LANGUAGE plpgsql;
