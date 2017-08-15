import psycopg2
import os
import sys
import json
import datetime
import decimal
import time
import base64
import io

class pg_encoder(json.JSONEncoder):
		def default(self, obj):
			if isinstance(obj, datetime.time) or isinstance(obj, datetime.datetime) or  isinstance(obj, datetime.date) or isinstance(obj, decimal.Decimal) or isinstance(obj, datetime.timedelta):
				return str(obj)
			return json.JSONEncoder.default(self, obj)

class pg_connection:
	def __init__(self, global_config ):
		self.global_conf=global_config
		self.pg_conn=self.global_conf.pg_conn
		self.pg_database=self.global_conf.pg_database
		if self.global_conf.schema_clear:
			self.dest_schema=self.global_conf.schema_clear
		else:
			self.dest_schema=self.global_conf.my_database
		if self.global_conf.schema_obf:
			self.schema_obf=self.global_conf.schema_obf
		else:
			self.schema_obf=self.dest_schema+"_obf"
		self.pg_connection=None
		self.pg_cursor=None
		self.pg_charset=self.global_conf.pg_charset
		
		
	
	def connect_db(self, destination_schema=None):
		pg_pars=dict(self.pg_conn.items()+ {'dbname':self.pg_database}.items())
		strconn="dbname=%(dbname)s user=%(user)s host=%(host)s password=%(password)s port=%(port)s"  % pg_pars
		self.pgsql_conn = psycopg2.connect(strconn)
		self.pgsql_conn .set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
		self.pgsql_conn .set_client_encoding(self.pg_charset)
		self.pgsql_cur=self.pgsql_conn .cursor()
		if destination_schema:
			self.dest_schema=destination_schema
			self.schema_obf=None
			
		
	
	def disconnect_db(self):
		self.pgsql_conn.close()
	
	def connect_replay_db(self):
		"""
			Connects to PostgreSQL using the parameters stored in pg_pars built adding the key dbname to the self.pg_conn dictionary.
			The method after the connection creates a database cursor and set the session to autocommit.
			This method creates an additional connection and cursor used by the replay process. 

		"""
		pg_pars=dict(list(self.pg_conn.items())+ list({'dbname':self.pg_database}.items()))
		strconn="dbname=%(dbname)s user=%(user)s host=%(host)s password=%(password)s port=%(port)s"  % pg_pars
		self.pgsql_conn_replay = psycopg2.connect(strconn)
		self.pgsql_conn_replay.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
		self.pgsql_conn_replay.set_client_encoding(self.pg_charset)
		self.pgsql_cur_replay=self.pgsql_conn_replay.cursor()
	
	def disconnect_replay_db(self):
		"""
			The method disconnects from the replay database connection.
		"""
		self.pgsql_conn_replay.close()

class pg_engine:
	def __init__(self, global_config, table_metadata,  logger, sql_dir='sql/'):
		self.lst_yes = global_config.lst_yes
		self.logger=logger
		self.sql_dir=sql_dir
		self.idx_sequence=0
		self.skip_view=global_config.skip_view
		self.pg_conn=pg_connection(global_config)
		self.pg_conn.connect_db()
		self.table_metadata=table_metadata
		self.type_dictionary = {
			'integer':'integer',
			'mediumint':'bigint',
			'tinyint':'integer',
			'smallint':'integer',
			'int':'integer',
			'bigint':'bigint',
			'varchar':'character varying',
			'text':'text',
			'char':'character',
			'datetime':'timestamp without time zone',
			'date':'date',
			'time':'time without time zone',
			'timestamp':'timestamp without time zone',
			'tinytext':'text',
			'mediumtext':'text',
			'longtext':'text',
			'tinyblob':'bytea',
			'mediumblob':'bytea',
			'longblob':'bytea',
			'blob':'bytea', 
			'binary':'bytea', 
			'varbinary':'bytea', 
			'decimal':'numeric', 
			'double':'double precision', 
			'double precision':'double precision', 
			'float':'double precision', 
			'bit':'integer', 
			'year':'integer', 
			'enum':'enum', 
			'set':'text', 
			'json':'text', 
			'bool':'boolean', 
			'boolean':'boolean', 
			'geometry':'bytea',
		}
		self.table_ddl={}
		self.idx_ddl={}
		self.type_ddl={}
		self.pg_charset=self.pg_conn.pg_charset
		self.batch_retention = global_config.batch_retention
		self.cat_version='0.19'
		self.cat_sql=[
			{'version':'base','script': 'create_schema.sql'}, 
			{'version':'0.8','script': 'upgrade/cat_0.8.sql'}, 
			{'version':'0.9','script': 'upgrade/cat_0.9.sql'}, 
			{'version':'0.10','script': 'upgrade/cat_0.10.sql'}, 
			{'version':'0.11','script': 'upgrade/cat_0.11.sql'}, 
			{'version':'0.12','script': 'upgrade/cat_0.12.sql'}, 
			{'version':'0.13','script': 'upgrade/cat_0.13.sql'}, 
			{'version':'0.14','script': 'upgrade/cat_0.14.sql'}, 
			{'version':'0.15','script': 'upgrade/cat_0.15.sql'}, 
			{'version':'0.16','script': 'upgrade/cat_0.16.sql'}, 
			{'version':'0.17','script': 'upgrade/cat_0.17.sql'}, 
			{'version':'0.18','script': 'upgrade/cat_0.18.sql'}, 
			{'version':'0.19','script': 'upgrade/cat_0.19.sql'}, 
			
		]
		cat_version=self.get_schema_version()
		num_schema=(self.check_service_schema())[0]
		if cat_version!=self.cat_version and int(num_schema)>0:
			self.upgrade_service_schema()
		self.table_limit = ['*']
		self.master_status = None
		
	
	
	def drop_obf_rel(self, relname, type):
		sql_unobf = """ DROP %s IF EXISTS "%s"."%s" CASCADE; """ % (type, self.pg_conn.schema_obf, relname)
		self.pg_conn.pgsql_cur.execute(sql_unobf)	
	
	def clear_obfuscation_reindex(self):
		self.logger.info("clearing existing idx definition for schema %s"  % (self.pg_conn.schema_obf))
		sql_del="""DELETE FROM sch_ninja.t_rebuild_idx;"""
		self.pg_conn.pgsql_cur.execute(sql_del)	
	
	def drop_null_obf(self):
		self.logger.info("dropping null constraints in schema %s"  % (self.pg_conn.schema_obf))
		sql_gen_drop="""
			WITH t_key as(
				SELECT
					sch.nspname schema_name,
					tab.relname table_name,
					att.attname column_name
				FROM
					pg_constraint con
					INNER JOIN pg_namespace sch
						ON sch.oid=connamespace
					INNER JOIN pg_attribute att
						ON
							att.attrelid=con.conrelid
						AND	att.attnum = any(con.conkey)
					INNER JOIN pg_class tab
						ON
							att.attrelid=tab.oid
				WHERE
					con.contype='p'
					and sch.nspname=%s
				)
				SELECT
					format(
							'ALTER TABLE %%I.%%I ALTER COLUMN %%I DROP NOT NULL;',
							table_schema,
							table_name,
							column_name
						) as drop_null
				FROM
					information_schema.columns col
				WHERE
						(
							table_schema,
							table_name,
							column_name
						) NOT IN
							(
								SELECT 
									schema_name,
									table_name,
									column_name 
								FROM 
									t_key
							)
					AND	table_schema=%s
					AND	Is_nullable='NO'
				;

		"""
		self.pg_conn.pgsql_cur.execute(sql_gen_drop, (self.obf_schema, self.obf_schema ))	
		null_cols=self.pg_conn.pgsql_cur.fetchall()
		for null_col in null_cols:
			try:
				self.pg_conn.pgsql_cur.execute(null_col [0])
			except psycopg2.Error as e:
				self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
				self.logger.error(null_col [0])
		
	
	def get_sync_tables(self, obfdic):
		obf_list = [tab for tab in obfdic]
		sql_get_tables = """
			SELECT
				table_name
			FROM
				information_schema.tables
			WHERE
					table_schema=%s
				AND table_name = ANY(%s)
			;
		"""
		
		sql_get_views = """
			SELECT
				table_name
			FROM
				information_schema.tables
			WHERE
					table_schema=%s
				AND table_name != ALL(%s)
			;
		"""
		self.pg_conn.pgsql_cur.execute(sql_get_tables, (self.dest_schema, obf_list))
		tab_clear = self.pg_conn.pgsql_cur.fetchall()
		self.sync_tables = [ tab[0] for tab in tab_clear if tab[0] in self.table_limit or  self.table_limit[0] == '*' ]
		
		self.pg_conn.pgsql_cur.execute(sql_get_views, (self.dest_schema, obf_list))
		views_clear = self.pg_conn.pgsql_cur.fetchall()
		self.sync_views = [ view[0] for view in views_clear if view[0] in self.table_limit or  self.table_limit[0] == '*' ]
		
		
		
	def refresh_views(self, obfdic):
		self.get_sync_tables(obfdic)
		for table in self.sync_views:
			self.logger.info("Processing view %s " % (table))
			try:
				obfdata = obfdic[table]
				self.logger.debug("Refreshing obfuscation for table %s " % (table))
				self.refresh_obf_table(table, obfdata)
			except:
				self.logger.debug("Table %s is not obfuscated. Refreshing the view" % (table))
				self.refresh_obf_view(table)
		
	def sync_obfuscation(self, obfdic):
		
		"""
			The method syncs the obfuscation schema using the schema in clear and the obfuscation dictionary
		"""
		self.get_sync_tables(obfdic)
		
		for table in self.sync_tables:
			self.logger.info("Processing table %s " % (table))
			try:
				obfdata = obfdic[table]
				self.logger.debug("Refreshing obfuscation for table %s " % (table))
				self.refresh_obf_table(table, obfdata)
			except:
				self.logger.info("Table %s is not obfuscated. Refreshing the view" % (table))
				self.refresh_obf_view(table)
		
		for table in self.sync_views:
			self.logger.info("Processing view %s " % (table))
			try:
				obfdata = obfdic[table]
				self.logger.debug("Refreshing obfuscation for table %s " % (table))
				self.refresh_obf_table(table, obfdata)
			except:
				self.logger.debug("Table %s is not obfuscated. Refreshing the view" % (table))
				self.refresh_obf_view(table)
		
	def refresh_obf_view(self, table):
		sql_drop_table = """ DROP TABLE IF EXISTS "%s"."%s" CASCADE;""" % (self.obf_schema, table)
		sql_drop_view = """ DROP VIEW IF EXISTS "%s"."%s" CASCADE;""" % (self.obf_schema, table)
		sql_create = """ CREATE OR REPLACE VIEW "%s"."%s" AS SELECT * FROM "%s"."%s";""" % (self.obf_schema, table, self.dest_schema, table)
		
		try:
			self.pg_conn.pgsql_cur.execute(sql_drop_table)
		except:
			pass
		
		try:
			self.logger.debug("Trying to replace the view %s" % (table))
			self.pg_conn.pgsql_cur.execute(sql_create)
			
		except:
			self.logger.warning("Running a drop/create for the view %s" % (table))
			try:
				self.pg_conn.pgsql_cur.execute(sql_drop_view)
				self.pg_conn.pgsql_cur.execute(sql_create)
			except:
				self.logger.error("Couldn't refresh the view %s" % (table))
		
	
		
	def refresh_obf_table(self, table, obfdata):
		sql_drop_table = """ DROP TABLE IF EXISTS "%s"."%s" CASCADE;""" % (self.obf_schema, table)
		sql_drop_view = """ DROP VIEW IF EXISTS "%s"."%s" CASCADE;""" % (self.obf_schema, table)

		sql_create_table = """
			CREATE TABLE "%s"."%s"
				(LIKE "%s"."%s")
		;
		""" % (self.obf_schema, table, self.dest_schema, table)
		try:
			self.pg_conn.pgsql_cur.execute(sql_drop_view)
		except:
			pass
		
		try:
			self.logger.debug("Trying to drop the table %s in schema %s " % (table, self.obf_schema))	
			self.pg_conn.pgsql_cur.execute(sql_drop_table)
			self.pg_conn.pgsql_cur.execute(sql_create_table)
			self.alter_obf_fields(table)
			self.copy_obf_data(table, obfdata)
			self.build_obf_idx(table)
		except:
			self.logger.error("Couldn't refresh the table %s" % (table))
			
			
	def build_obf_idx(self, table):
		sql_get_idx = """
			SELECT 
				CASE
					WHEN indisprimary
					THEN 
						format(
							'ALTER TABLE %%I.%%I ADD CONSTRAINT %%I PRIMARY KEY(%%s);',
							sch_obf,
							table_name,
							index_name,
							column_names
							
						)
					WHEN indisunique
					THEN 
						format(
							'CREATE UNIQUE INDEX %%I ON %%I.%%I (%%s);',
							index_name,
							sch_obf,
							table_name,
							column_names
						)
					ELSE
						format(
							'CREATE INDEX %%I ON %%I.%%I (%%s);',
							index_name,
							sch_obf,
							table_name,
							column_names
						)
					
				
				END as create_idx
			FROM
			(
				SELECT 
					tab.relname as table_name,
					sch.nspname as schema_name,
					idx.relname as index_name,
					idx_tab.indisprimary,
					idx_tab.indisunique,
					string_agg(quote_ident(col.attname),',') as column_names
				FROM 
					pg_class tab
					INNER JOIN pg_namespace sch
					ON
						sch.oid=tab.relnamespace
					INNER JOIN 
						(
							SELECT
								indisprimary,
								indisunique,
								unnest(indkey) as indkey,
								indrelid,
								indexrelid
							FROM
								pg_index
						) idx_tab
						ON tab.oid=idx_tab.indrelid
					INNER JOIN pg_class idx
						ON idx_tab.indexrelid=idx.oid
					INNER JOIN pg_attribute col
						ON 
								tab.oid=col.attrelid
							AND	col.attnum = idx_tab.indkey
				WHERE
						tab.relname=%s
					AND	sch.nspname=%s
					AND 	col.attnum>0
				GROUP BY 
					tab.relname,
					sch.nspname,
					idx.relname,
					idx_tab.indisprimary,
					idx_tab.indisunique
				
			) t_idx,
			(
				SELECT
					%s AS sch_obf
			) t_obf

			;

		"""
		self.pg_conn.pgsql_cur.execute(sql_get_idx, (table, self.dest_schema, self.obf_schema ) )
		build_idx = self.pg_conn.pgsql_cur.fetchall()
		build_idx = [ idx[0] for idx in build_idx ]
		for idx in build_idx:
			try:
				self.logger.info("Executing: %s" % (idx))
				self.pg_conn.pgsql_cur.execute(idx)
			except:
				self.logger.error("Couldn't add the index to the table %s. \nIndex definition: %s" % (table, idx))
			
		
	def create_obf_child(self, table):
		sql_check="""SELECT 
									count(*) 
								FROM
									information_schema.tables 
								WHERE 
													table_schema=%s 
										AND 	table_name=%s;
						"""
		self.pg_conn.pgsql_cur.execute(sql_check, (self.dest_schema, table))	
		tab_count=self.pg_conn.pgsql_cur.fetchone()
		if tab_count[0]>0:
			sql_child="""
				DROP TABLE  IF EXISTS \"""" + self.obf_schema + """\".\"""" + table + """\" ; 
				CREATE TABLE \"""" + self.obf_schema + """\".\"""" + table + """\"  
				(LIKE \"""" + self.dest_schema + """\".\"""" + table + """\")
				;
			"""
			self.pg_conn.pgsql_cur.execute(sql_child)
			self.alter_obf_fields(table)
			return True
		else:
			return False
	
	def alter_obf_fields(self, table):
		""" """
		sql_alter="""
			WITH 
				t_filter AS
					(
						SELECT
						%s::text AS table_schema,
						%s::text AS table_name
					),
					t_key AS
					(
						SELECT 
						column_name 
						FROM
							information_schema.key_column_usage keycol 
						INNER JOIN t_filter fil
						ON 
							keycol.table_schema=fil.table_schema
							AND keycol.table_name=fil.table_name
					)

				SELECT 
					format('ALTER TABLE %%I.%%I ALTER COLUMN %%I TYPE text ;',
					col.table_schema,
					col.table_name,
					col.column_name
					) AS alter_table
				FROM
					information_schema.columns col
					INNER JOIN t_filter fil
					    ON
						col.table_schema=fil.table_schema
					    AND col.table_name=fil.table_name
				WHERE 
				     column_name NOT IN (
							SELECT 
								column_name 
							    FROM
								t_key
							       )
					 AND data_type = 'character varying'
				UNION ALL

				SELECT 
				    format('ALTER TABLE %%I.%%I ALTER COLUMN %%I DROP NOT NULL;',
					col.table_schema,
					col.table_name,
					col.column_name
					) AS alter_table
				FROM
					information_schema.columns col
					INNER JOIN t_filter fil
					    ON 
						col.table_schema=fil.table_schema
					    AND col.table_name=fil.table_name
				WHERE 
				     column_name NOT IN (
							    SELECT 
								column_name 
							    FROM
								t_key
							       )
					 AND is_nullable = 'NO'
				;
		"""
		self.pg_conn.pgsql_cur.execute(sql_alter, (self.obf_schema, table, ))
		alter_stats = self.pg_conn.pgsql_cur.fetchall()
		for alter in alter_stats:
			self.pg_conn.pgsql_cur.execute(alter[0])
	
	def copy_obf_data(self, table, obfdic):
		sql_crypto="SELECT count(*) FROM pg_catalog.pg_extension where extname='pgcrypto';"
		self.pg_conn.pgsql_cur.execute(sql_crypto)
		pg_crypto=self.pg_conn.pgsql_cur.fetchone()
		if pg_crypto[0] == 0:
			self.logger.info("extension pgcrypto missing on database. falling back to md5 obfuscation")
		col_list=[]
		sql_cols=""" 
					SELECT
						column_name,
						CASE
							WHEN 
								character_maximum_length IS NOT NULL 
							THEN
								format('::%%s(%%s)',data_type,character_maximum_length) 
							ELSE
								format('::%%s',data_type)
						END AS data_cast
					FROM
						information_schema.COLUMNS
					WHERE 
								table_schema=%s
						AND table_name=%s
					ORDER BY 
						ordinal_position 
					;
			"""
		self.pg_conn.pgsql_cur.execute(sql_cols, (self.pg_conn.dest_schema,table ))
		columns=self.pg_conn.pgsql_cur.fetchall()
		for column in columns:
			try:
				obfdata=obfdic[column[0]]
				if obfdata["mode"]=="normal":
					if pg_crypto[0] == 0:
						col_list.append("(substr(\"%s\"::text, %s , %s)||md5(\"%s\"))%s" %(column[0], obfdata["nonhash_start"], obfdata["nonhash_length"], column[0],  column[1]))
					else:
						col_list.append("(substr(\"%s\"::text, %s , %s)||encode(public.digest(\"%s\",'sha256'),'hex'))%s" %(column[0], obfdata["nonhash_start"], obfdata["nonhash_length"], column[0],  column[1]))

				elif obfdata["mode"]=="date":
					col_list.append("to_char(\"%s\"::date,'YYYY-01-01')::date" % (column[0]))
				elif obfdata["mode"] == "numeric":
					col_list.append("0%s" % (column[1]))
				elif obfdata["mode"] == "setnull":
					col_list.append("NULL%s" % (column[1]))
			except:
				col_list.append('"%s"'%(column[0], ))
				
		tab_exists=self.truncate_table(table, self.obf_schema)
		if tab_exists:
			sql_insert="""INSERT INTO  \"""" + self.obf_schema + """\".\"""" + table + """\"  SELECT """ + ','.join(col_list) + """ FROM  \"""" + self.pg_conn.dest_schema + """\".\"""" + table + """\" ;"""
			self.logger.debug("copying table: %s in obfuscated schema" % (table, ))
			self.pg_conn.pgsql_cur.execute(sql_insert)
			
	def create_views(self, obfdic):
		self.logger.info("creating views for tables not in obfuscation list")
		table_obf=[table for table in obfdic]
		if self.skip_view:
			table_obf = table_obf + self.skip_view
		
		sql_create="""
					SELECT 
							format('CREATE OR REPLACE VIEW %%I.%%I AS SELECT * FROM %%I.%%I ;',
							%s,
							table_name,
							table_schema,
							table_name
							) as create_view,
							table_name,
							table_schema,
							format('DROP VIEW IF EXISTS %%I.%%I CASCADE;',
							%s,
							table_name
							) as drop_view
					FROM
						information_schema.TABLES 
					WHERE 
					table_schema=%s
					AND table_name not in (SELECT unnest(%s))
					;
				"""
		self.pg_conn.pgsql_cur.execute(sql_create, (self.obf_schema,self.obf_schema, self.dest_schema, table_obf, ))
		create_views=self.pg_conn.pgsql_cur.fetchall()
		for statement in create_views:
			try:
				self.pg_conn.pgsql_cur.execute(statement[3])
				self.pg_conn.pgsql_cur.execute(statement[0])
			except psycopg2.Error as e:
				if e.pgcode == '42809':
					self.logger.info("replacing table %s in schema %s with a view. old table is renamed to %s_bak" % (self.obf_schema, statement[2],   statement[1]))
					sql_rename="""ALTER TABLE "%s"."%s" RENAME TO "%s_bak" ;""" % (self.obf_schema,  statement[1], statement[1])
					self.pg_conn.pgsql_cur.execute(sql_rename)
					self.pg_conn.pgsql_cur.execute(statement[0])
				else:
					self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
					self.logger.error(statement[3]+statement[0])


	def copy_obfuscated(self, obfdic, tables_limit):
		table_obf={}
		if tables_limit:
			for table in tables_limit:
				try:
					table_obf[table]=obfdic[table]
				except:
					pass
		else:
			table_obf=obfdic
		for table in table_obf:
			if self.create_obf_child(table):
				self.copy_obf_data(table, table_obf[table])
	
	def create_schema(self):
		
		if self.obf_schema:
			sql_schema=" CREATE SCHEMA IF NOT EXISTS "+self.obf_schema+";"
			self.pg_conn.pgsql_cur.execute(sql_schema)
		sql_schema=" CREATE SCHEMA IF NOT EXISTS "+self.dest_schema+";"
		sql_path=" SET search_path="+self.dest_schema+";"
		self.pg_conn.pgsql_cur.execute(sql_schema)
		self.pg_conn.pgsql_cur.execute(sql_path)
	
		
	def store_table(self, table_name):
		"""
			The method saves the table name along with the primary key definition in the table t_replica_tables.
			This is required in order to let the replay procedure which primary key to use replaying the update and delete.
			If the table is without primary key is not stored. 
			A table without primary key is copied and the indices are create like any other table. 
			However the replica doesn't work for the tables without primary key.
			
			If the class variable master status is set then the master's coordinates are saved along with the table.
			This happens in general when a table is added to the replica or the data is refreshed with sync_tables.
			
			
			:param table_name: the table name to store in the table  t_replica_tables
		"""
		if self.master_status:
			master_data = self.master_status[0]
			binlog_file = master_data["File"]
			binlog_pos = master_data["Position"]
		else:
			binlog_file = None
			binlog_pos = None
		table_data=self.table_metadata[table_name]
		table_no_pk = True
		for index in table_data["indices"]:
			if index["index_name"]=="PRIMARY":
				table_no_pk = False
				sql_insert=""" 
					INSERT INTO sch_ninja.t_replica_tables 
						(
							i_id_source,
							v_table_name,
							v_schema_name,
							v_table_pkey,
							t_binlog_name,
							i_binlog_position
						)
					VALUES 
						(
							%s,
							%s,
							%s,
							ARRAY[%s],
							%s,
							%s
						)
					ON CONFLICT (i_id_source,v_table_name,v_schema_name)
						DO UPDATE 
							SET 
								v_table_pkey=EXCLUDED.v_table_pkey,
								t_binlog_name = EXCLUDED.t_binlog_name,
								i_binlog_position = EXCLUDED.i_binlog_position
										;
								"""
				self.pg_conn.pgsql_cur.execute(sql_insert, (
					self.i_id_source, 
					table_name, 
					self.dest_schema, 
					index["index_columns"].strip(), 
					binlog_file, 
					binlog_pos
					)
				)
				self.pg_conn.pgsql_cur.execute(sql_insert, (
					self.i_id_source, 
					table_name, 
					self.obf_schema, 
					index["index_columns"].strip(), 
					binlog_file, 
					binlog_pos
					)
				)
		if table_no_pk:
			sql_delete = """
				DELETE FROM sch_ninja.t_replica_tables
				WHERE
						i_id_source=%s
					AND	v_table_name=%s
					AND	v_schema_name=%s
				;
			"""
			self.pg_conn.pgsql_cur.execute(sql_delete, (
				self.i_id_source, 
				table_name, 
				self.dest_schema)
				)
			self.pg_conn.pgsql_cur.execute(sql_delete, (
				self.i_id_source, 
				table_name, 
				self.obf_schema)
				)

		
	
	def unregister_table(self, table_name):
		self.logger.info("unregistering table %s from the replica catalog" % (table_name,))
		sql_delete=""" DELETE FROM sch_ninja.t_replica_tables 
									WHERE
											v_table_name=%s
										AND	v_schema_name=%s
								RETURNING i_id_table
								;
						"""
		self.pg_conn.pgsql_cur.execute(sql_delete, (table_name, self.dest_schema))	
		removed_id=self.pg_conn.pgsql_cur.fetchone()
		table_id=removed_id[0]
		self.logger.info("renaming table %s to %s_%s" % (table_name, table_name, table_id))
		sql_rename="""ALTER TABLE IF EXISTS "%s"."%s" rename to "%s_%s"; """ % (self.dest_schema, table_name, table_name, table_id)
		self.logger.debug(sql_rename)
		self.pg_conn.pgsql_cur.execute(sql_rename)	
	
	def create_tables(self, drop_tables=False, store_tables=True):
			for table in self.table_ddl:
				if drop_tables:
					sql_drop_clear='DROP TABLE IF EXISTS  "%s"."%s" CASCADE ;' % (self.pg_conn.dest_schema, table,)
					sql_drop_obf='DROP TABLE IF EXISTS  "%s"."%s" CASCADE ;' % (self.obf_schema, table,)
					self.pg_conn.pgsql_cur.execute(sql_drop_clear)
					self.pg_conn.pgsql_cur.execute(sql_drop_obf)
				try:
					ddl_enum=self.type_ddl[table]
					for sql_type in ddl_enum:
						self.pg_conn.pgsql_cur.execute(sql_type)
				except:
					pass
				sql_create=self.table_ddl[table]
				try:
					self.pg_conn.pgsql_cur.execute(sql_create)
				except psycopg2.Error as e:
					self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
					self.logger.error(sql_create)
				self.logger.debug('Storing table %s in t_replica_tables' % (table, ))
				if store_tables:
					self.store_table(table)
	
	def create_indices(self):
		self.logger.info("creating the indices")
		for index in self.idx_ddl:
			idx_ddl= self.idx_ddl[index]
			for sql_idx in idx_ddl:
				self.pg_conn.pgsql_cur.execute(sql_idx)
	
	def reset_sequences(self, destination_schema):
		""" method to reset the sequences to the max value available in table """
		self.logger.info("resetting the sequences in schema %s" % destination_schema)
		sql_gen_reset=""" SELECT 
													format('SELECT setval(%%L::regclass,(select max(id) FROM %%I.%%I));',
														replace(replace(column_default,'nextval(''',''),'''::regclass)',''),
														table_schema,
														table_name
													)
									FROM 
										information_schema.columns
									WHERE 
											table_schema=%s
										AND column_default like 'nextval%%'
								;"""
		self.pg_conn.pgsql_cur.execute(sql_gen_reset, (destination_schema, ))
		results=self.pg_conn.pgsql_cur.fetchall()
		try:
			for statement in results[0]:
				self.pg_conn.pgsql_cur.execute(statement)
		except psycopg2.Error as e:
					self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
					self.logger.error(statement)
		except:
			pass
			
	def copy_data(self, table,  csv_file,  my_tables={}):
		column_copy=[]
		for column in my_tables[table]["columns"]:
			column_copy.append('"'+column["column_name"]+'"')
		sql_copy="COPY "+'"'+self.dest_schema+'"'+"."+'"'+table+'"'+" ("+','.join(column_copy)+") FROM STDIN WITH NULL 'NULL' CSV QUOTE '\"' DELIMITER',' ESCAPE '\"' ; "
		self.pg_conn.pgsql_cur.copy_expert(sql_copy,csv_file)
	
		
	def insert_data(self, table,  insert_data,  my_tables={}):
		column_copy=[]
		column_marker=[]
		
		for column in my_tables[table]["columns"]:
			column_copy.append('"'+column["column_name"]+'"')
			column_marker.append('%s')
		sql_head="INSERT INTO "+'"'+self.pg_conn.dest_schema+'"'+"."+'"'+table+'"'+" ("+','.join(column_copy)+") VALUES ("+','.join(column_marker)+");"
		for data_row in insert_data:
			column_values=[]
			for column in my_tables[table]["columns"]:
				column_values.append(data_row[column["column_name"]])
			try:
				self.pg_conn.pgsql_cur.execute(sql_head,column_values)	
			except psycopg2.Error as e:
					self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
					self.logger.error(self.pg_conn.pgsql_cur.mogrify(sql_head,column_values))
			except:
				self.logger.error("unexpected error when processing the row")
				self.logger.error(" - > Table: %s" % table)
				self.logger.error(" - > Insert list: %s" % (','.join(column_copy)) )
				self.logger.error(" - > Insert values: %s" % (column_values) )
				
	def build_tab_ddl(self):
		""" 
			The method iterates over the list l_tables and builds a new list with the statements for tables
		"""
		if self.table_limit[0] != '*' :
			table_metadata = {}
			for tab in self.table_limit:
				try:
					table_metadata[tab] = self.table_metadata[tab]
				except:
					pass
		else:
			table_metadata = self.table_metadata
		
		for table_name in table_metadata:
			table=self.table_metadata[table_name]
			columns=table["columns"]
			
			ddl_head="CREATE TABLE "+'"'+table["name"]+'" ('
			ddl_tail=");"
			ddl_columns=[]
			ddl_enum=[]
			for column in columns:
				if column["is_nullable"]=="NO":
					col_is_null="NOT NULL"
				else:
					col_is_null="NULL"
				column_type=self.type_dictionary[column["data_type"]]
				if column_type=="enum":
					enum_type="enum_"+table["name"]+"_"+column["column_name"]
					sql_drop_enum='DROP TYPE IF EXISTS '+enum_type+' CASCADE;'
					sql_create_enum="CREATE TYPE "+enum_type+" AS ENUM "+column["enum_list"]+";"
					ddl_enum.append(sql_drop_enum)
					ddl_enum.append(sql_create_enum)
					column_type=enum_type
				if column_type=="character varying" or column_type=="character":
					column_type=column_type+"("+str(column["character_maximum_length"])+")"
				if column_type=='numeric':
					column_type=column_type+"("+str(column["numeric_precision"])+","+str(column["numeric_scale"])+")"
				if column["extra"]=="auto_increment":
					column_type="bigserial"
				ddl_columns.append('"'+column["column_name"]+'" '+column_type+" "+col_is_null )
			def_columns=str(',').join(ddl_columns)
			self.type_ddl[table["name"]]=ddl_enum
			self.table_ddl[table["name"]]=ddl_head+def_columns+ddl_tail

	def drop_tables(self):
		"""
			The method drops the tables present in the table_ddl
		"""
		self.set_search_path()
		for table in self.table_ddl:
			self.logger.debug("dropping table %s " % (table, ))
			sql_drop = """DROP TABLE IF EXISTS "%s"  CASCADE;""" % (table, )
			self.pg_conn.pgsql_cur.execute(sql_drop)
	
	def set_search_path(self):
		"""
			The method sets the search path for the connection.
		"""
		sql_path=" SET search_path=%s;" % (self.dest_schema, )
		self.pg_conn.pgsql_cur.execute(sql_path)
	
	def build_idx_ddl(self, obfdic={}):
		""" the function iterates over the list l_pkeys and builds a new list with the statements for pkeys """
		if self.table_limit[0] != '*' :
			table_metadata = {}
			for tab in self.table_limit:
				try:
					table_metadata[tab] = self.table_metadata[tab]
				except:
					pass
		else:
			table_metadata = self.table_metadata
		table_obf=[table for table in obfdic]
		for table_name in table_metadata:
			table=table_metadata[table_name]
			
			table_name=table["name"]
			indices=table["indices"]
			table_idx=[]
			for index in indices:
				indx=index["index_name"]
				index_columns=index["index_columns"]
				non_unique=index["non_unique"]
				if indx=='PRIMARY':
					pkey_name="pk_"+table_name[0:20]+"_"+str(self.idx_sequence)
					pkey_def='ALTER TABLE "'+table_name+'" ADD CONSTRAINT "'+pkey_name+'" PRIMARY KEY ('+index_columns+') ;'
					table_idx.append(pkey_def)
					if table_name in table_obf:
						pkey_def='ALTER TABLE "'+self.obf_schema+'"."'+table_name+'" ADD CONSTRAINT "'+pkey_name+'" PRIMARY KEY ('+index_columns+') ;'
						table_idx.append(pkey_def)
				else:
					if non_unique==0:
						unique_key='UNIQUE'
					else:
						unique_key=''
					index_name='"idx_'+indx[0:20]+table_name[0:20]+"_"+str(self.idx_sequence)+'"'
					idx_def='CREATE '+unique_key+' INDEX '+ index_name+' ON "'+table_name+'" ('+index_columns+');'
					table_idx.append(idx_def)
					if table_name in table_obf:
						idx_def='CREATE '+unique_key+' INDEX '+ index_name+' ON "'+self.obf_schema+'"."'+table_name+'" ('+index_columns+');'
						table_idx.append(idx_def)
						
				self.idx_sequence+=1
					
			self.idx_ddl[table_name]=table_idx
		
	def get_schema_version(self):
		"""
			Gets the service schema version.
		"""
		sql_check="""
			SELECT 
				t_version
			FROM 
				sch_ninja.v_version 
			;
		"""
		try:
			self.pg_conn.pgsql_cur.execute(sql_check)
			value_check=self.pg_conn.pgsql_cur.fetchone()
			cat_version=value_check[0]
		except:
			cat_version='base'
		return cat_version
		
	def upgrade_service_schema(self):
		"""
			Upgrade the service schema to the latest version using the upgrade files
		"""
		
		self.logger.info("Upgrading the service schema")
		install_script=False
		cat_version=self.get_schema_version()
		for install in self.cat_sql:
			script_ver=install["version"]
			script_schema=install["script"]
			self.logger.info("script schema %s, detected schema version %s - target version: %s - install_script:%s " % (script_ver, cat_version, self.cat_version,  install_script))
			if install_script==True:
				sql_view="""
					CREATE OR REPLACE VIEW sch_ninja.v_version 
						AS
							SELECT %s::TEXT t_version
					;"""
				self.logger.info("Installing file version %s" % (script_ver, ))
				file_schema=open(self.sql_dir+script_schema, 'rb')
				sql_schema=file_schema.read()
				file_schema.close()
				self.pg_conn.pgsql_cur.execute(sql_schema)
				self.pg_conn.pgsql_cur.execute(sql_view, (script_ver, ))
				if script_ver=='0.9':
						sql_update="""
							UPDATE sch_ninja.t_sources
							SET
								t_dest_schema=%s,
								t_obf_schema=%s
							WHERE i_id_source=(
												SELECT 
													i_id_source
												FROM
													sch_ninja.t_sources
												WHERE
													t_source='default'
													AND t_dest_schema='default'
													AND t_obf_schema='default'
											)
							;
						"""
						self.pg_conn.pgsql_cur.execute(sql_update, (self.pg_conn.dest_schema,self.pg_conn.schema_obf ))
			if script_ver==cat_version and not install_script:
				self.logger.info("enabling install script")
				install_script=True
				
	def check_service_schema(self):
		sql_check="""
								SELECT 
									count(*)
								FROM 
									information_schema.schemata  
								WHERE 
									schema_name='sch_ninja'
						"""
			
		self.pg_conn.pgsql_cur.execute(sql_check)
		num_schema=self.pg_conn.pgsql_cur.fetchone()
		return num_schema
	
	def create_service_schema(self):
		
		num_schema=self.check_service_schema()
		if num_schema[0]==0:
			for install in self.cat_sql:
				script_ver=install["version"]
				script_schema=install["script"]
				if script_ver=='base':
					self.logger.info("Installing service schema %s" % (script_ver, ))
					file_schema=open(self.sql_dir+script_schema, 'rb')
					sql_schema=file_schema.read()
					file_schema.close()
					self.pg_conn.pgsql_cur.execute(sql_schema)
		else:
			self.logger.error("The service schema is already created")
			
		
	def drop_service_schema(self):
		file_schema=open(self.sql_dir+"drop_schema.sql", 'rb')
		sql_schema=file_schema.read()
		file_schema.close()
		self.pg_conn.pgsql_cur.execute(sql_schema)
	
	def save_master_status(self, master_status, cleanup=False):
		"""
			This method saves the master data determining which log table should be used in the next batch.
			
			The method performs also a cleanup for the logged events the cleanup parameter is true.
			
			:param master_status: the master data with the binlogfile and the log position
			:param cleanup: if true cleans the not replayed batches. This is useful when resyncing a replica.
		"""
		next_batch_id=None
		master_data = master_status[0]
		binlog_name = master_data["File"]
		binlog_position = master_data["Position"]
		try:
			event_time = master_data["Time"]
		except:
			event_time = None
		
		sql_master="""
			INSERT INTO sch_ninja.t_replica_batch
				(
					i_id_source,
					t_binlog_name, 
					i_binlog_position
				)
			VALUES 
				(
					%s,
					%s,
					%s
				)
			RETURNING i_id_batch
			;
		"""
						
		sql_event="""
			UPDATE sch_ninja.t_sources 
			SET 
				ts_last_received=to_timestamp(%s),
				v_log_table=ARRAY[v_log_table[2],v_log_table[1]]
				
			WHERE 
				i_id_source=%s
			RETURNING v_log_table[1]
			; 
		"""
		
		self.logger.info("saving master data id source: %s log file: %s  log position:%s Last event: %s" % (self.i_id_source, binlog_name, binlog_position, event_time))
		
		
		try:
			if cleanup:
				self.logger.info("cleaning not replayed batches for source %s", self.i_id_source)
				sql_cleanup=""" DELETE FROM sch_ninja.t_replica_batch WHERE i_id_source=%s AND NOT b_replayed; """
				self.pg_conn.pgsql_cur.execute(sql_cleanup, (self.i_id_source, ))
			self.pg_conn.pgsql_cur.execute(sql_master, (self.i_id_source, binlog_name, binlog_position))
			results=self.pg_conn.pgsql_cur.fetchone()
			next_batch_id=results[0]
		except psycopg2.Error as e:
					self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
					self.logger.error(self.pg_conn.pgsql_cur.mogrify(sql_master, (self.i_id_source, binlog_name, binlog_position)))
		try:
			self.pg_conn.pgsql_cur.execute(sql_event, (event_time, self.i_id_source, ))
			results = self.pg_conn.pgsql_cur.fetchone()
			table_file = results[0]
			self.logger.debug("master data: table file %s, log name: %s, log position: %s " % (table_file, binlog_name, binlog_position))
		
		
			
		except psycopg2.Error as e:
					self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
					self.pg_conn.pgsql_cur.mogrify(sql_event, (event_time, self.i_id_source, ))
		
		return next_batch_id
		
		
		
	def get_batch_data(self):
		"""
			The method updates the batch status to started for the given source_id and returns the 
			batch informations.
			
			:return: psycopg2 fetchall results without any manipulation
			:rtype: psycopg2 tuple
			
		"""
		sql_batch="""
			WITH t_created AS
				(
					SELECT 
						max(ts_created) AS ts_created
					FROM 
						sch_ninja.t_replica_batch  
					WHERE 
							NOT b_processed
						AND	NOT b_replayed
						AND	i_id_source=%s
				)
			UPDATE sch_ninja.t_replica_batch
			SET 
				b_started=True
			FROM 
				t_created
			WHERE
					t_replica_batch.ts_created=t_created.ts_created
				AND	i_id_source=%s
			RETURNING
				i_id_batch,
				t_binlog_name,
				i_binlog_position,
				(SELECT v_log_table[1] from sch_ninja.t_sources WHERE i_id_source=%s) as v_log_table
				
			;
		"""
		self.pg_conn.pgsql_cur.execute(sql_batch, (self.i_id_source, self.i_id_source, self.i_id_source, ))
		return self.pg_conn.pgsql_cur.fetchall()	
	
	def save_discarded_row(self,row_data,batch_id):
		print(str(row_data))
		b64_row=base64.b64encode(str(row_data))
		sql_save="""INSERT INTO sch_ninja.t_discarded_rows(
											i_id_batch, 
											t_row_data
											)
						VALUES (%s,%s);
						"""
		self.pg_conn.pgsql_cur.execute(sql_save,(batch_id,b64_row))
	
	def write_batch(self, group_insert):
		csv_file=io.StringIO()
		
		insert_list=[]
		for row_data in group_insert:
			global_data=row_data["global_data"]
			event_data=row_data["event_data"]
			event_update=row_data["event_update"]
			log_table=global_data["log_table"]
			insert_list.append(self.pg_conn.pgsql_cur.mogrify("%s,%s,%s,%s,%s,%s,%s,%s,%s" ,  (
						global_data["batch_id"], 
						global_data["table"],  
						global_data["schema"], 
						global_data["action"], 
						global_data["binlog"], 
						global_data["logpos"], 
						json.dumps(event_data, cls=pg_encoder), 
						json.dumps(event_update, cls=pg_encoder), 
						global_data["event_time"], 
						
					)
				)
			)
											
		csv_data=b"\n".join(insert_list ).decode()
		csv_file.write(csv_data)
		csv_file.seek(0)
		try:
			
			#self.pg_conn.pgsql_cur.execute(sql_insert)
			sql_copy="""COPY "sch_ninja"."""+log_table+""" (
									i_id_batch, 
									v_table_name, 
									v_schema_name, 
									enm_binlog_event, 
									t_binlog_name, 
									i_binlog_position, 
									jsb_event_data,
									jsb_event_update,
									i_my_event_time
									
								) FROM STDIN WITH NULL 'NULL' CSV QUOTE '''' DELIMITER ',' ESCAPE '''' ; """
			self.pg_conn.pgsql_cur.copy_expert(sql_copy,csv_file)
		except psycopg2.Error as e:
			self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
			self.logger.error(csv_data)
			self.logger.error("fallback to inserts")
			self.insert_batch(group_insert)
	
	def insert_batch(self,group_insert):
		self.logger.debug("starting insert loop")
		for row_data in group_insert:
			global_data=row_data["global_data"]
			event_data=row_data["event_data"]
			event_update=row_data["event_update"]
			log_table=global_data["log_table"]
			event_time = global_data["event_time"]
			
			sql_insert="""
				INSERT INTO sch_ninja."""+log_table+"""
				(
					i_id_batch, 
					v_table_name, 
					v_schema_name, 
					enm_binlog_event, 
					t_binlog_name, 
					i_binlog_position, 
					jsb_event_data,
					jsb_event_update,
					i_my_event_time
				)
				VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
				;						
			"""
			try:
				self.pg_conn.pgsql_cur.execute(sql_insert,(
					global_data["batch_id"], 
					global_data["table"],  
					global_data["schema"], 
					global_data["action"], 
					global_data["binlog"], 
					global_data["logpos"], 
					json.dumps(event_data, cls=pg_encoder), 
					json.dumps(event_update, cls=pg_encoder)), 
					event_time
				)
			except:
				self.logger.error("error when storing event data. saving the discarded row")
				self.save_discarded_row(row_data,global_data["batch_id"])
	
	def set_batch_processed(self, id_batch):
		"""
			The method updates the flag b_processed and sets the processed timestamp for the given batch id
			
			:param id_batch: the id batch to set as processed
		"""
		self.logger.debug("updating batch %s to processed" % (id_batch, ))
		sql_update=""" 
			UPDATE sch_ninja.t_replica_batch
				SET
					b_processed=True,
					ts_processed=now()
			WHERE
				i_id_batch=%s
			;
		"""
		self.pg_conn.pgsql_cur.execute(sql_update, (id_batch, ))
		self.logger.debug("collecting events id for batch %s " % (id_batch, ))
		sql_collect_events = """
			INSERT INTO
				sch_ninja.t_batch_events
				(
					i_id_batch,
					i_id_event
				)
			SELECT
				i_id_batch,
				array_agg(i_id_event)
			FROM
			(
				SELECT 
					i_id_batch,
					i_id_event,
					ts_event_datetime
				FROM 
					sch_ninja.t_log_replica 
				WHERE i_id_batch=%s
				ORDER BY ts_event_datetime
			) t_event
			GROUP BY
					i_id_batch
			;
		"""
		self.pg_conn.pgsql_cur.execute(sql_collect_events, (id_batch, ))

	def process_batch(self, replica_batch_size):
		self.logger.debug("Replay batch in %s row chunks" % (replica_batch_size, ))
		batch_loop=True
		sql_process="""SELECT sch_ninja.fn_process_batch(%s,%s);"""
		while batch_loop:
			try:
				self.pg_conn.pgsql_cur_replay.execute(sql_process, (replica_batch_size, self.i_id_source))
				batch_result=self.pg_conn.pgsql_cur_replay.fetchone()
				batch_loop=batch_result[0]
			except:
				self.pg_conn.connect_replay_db()
				
			self.logger.debug("Batch loop value %s" % (batch_loop))
		self.logger.debug("Cleaning replayed batches older than %s for source %s" % (self.batch_retention, self.i_id_source))
		sql_cleanup="""DELETE FROM 
									sch_ninja.t_replica_batch
								WHERE
										b_started
									AND b_processed
									AND b_replayed
									AND now()-ts_replayed>%s::interval
									AND i_id_source=%s
									 """
		self.pg_conn.pgsql_cur_replay.execute(sql_cleanup, (self.batch_retention, self.i_id_source))


	def build_alter_table(self, token):
		""" 
			The method builds the alter table statement from the token data.
			The function currently supports the following statements.
			DROP TABLE
			ADD COLUMN 
			CHANGE
			MODIFY
			
			The change and modify are potential source of breakage for the replica because of 
			the mysql implicit fallback data types. 
			For better understanding please have a look to 
			
			http://www.cybertec.at/why-favor-postgresql-over-mariadb-mysql/
			
			:param token: A dictionary with the tokenised sql statement
			:return: query the DDL query in the PostgreSQL dialect
			:rtype: string
			
		"""
		alter_cmd=[]
		ddl_enum=[]
		query_cmd=token["command"]
		table_name=token["name"]
		for alter_dic in token["alter_cmd"]:
			if alter_dic["command"] == 'DROP':
				alter_cmd.append("%(command)s \"%(name)s\" CASCADE" % alter_dic)
			elif alter_dic["command"] == 'ADD':
				column_type=self.type_dictionary[alter_dic["type"]]
				if column_type=="enum":
					enum_name="enum_"+table_name+"_"+alter_dic["name"]
					column_type=enum_name
					sql_drop_enum='DROP TYPE IF EXISTS '+column_type+' CASCADE;'
					sql_create_enum="CREATE TYPE "+column_type+" AS ENUM ("+alter_dic["dimension"]+");"
					ddl_enum.append(sql_drop_enum)
					ddl_enum.append(sql_create_enum)
				if column_type=="character varying" or column_type=="character" or column_type=='numeric' or column_type=='bit' or column_type=='float':
						column_type=column_type+"("+str(alter_dic["dimension"])+")"
				alter_cmd.append("%s \"%s\" %s NULL" % (alter_dic["command"], alter_dic["name"], column_type))	
			elif alter_dic["command"] == 'CHANGE':
				sql_rename = ""
				sql_type = ""
				old_column=alter_dic["old"]
				new_column=alter_dic["new"]
				column_type=self.type_dictionary[alter_dic["type"]]
				if column_type=="character varying" or column_type=="character" or column_type=='numeric' or column_type=='bit' or column_type=='float':
						column_type=column_type+"("+str(alter_dic["dimension"])+")"
				sql_type = """ALTER TABLE "%s" ALTER COLUMN "%s" SET DATA TYPE %s  USING "%s"::%s ;;""" % (table_name, old_column, column_type, old_column, column_type)
				if old_column != new_column:
					sql_rename="""ALTER TABLE  "%s" RENAME COLUMN "%s" TO "%s" ;""" % (table_name, old_column, new_column)
				query=sql_type+sql_rename
				return query
			elif alter_dic["command"] == 'MODIFY':
				column_type=self.type_dictionary[alter_dic["type"]]
				column_name=alter_dic["name"]
				if column_type=="enum":
					enum_name="enum_"+table_name+"_"+alter_dic["name"]
					column_type=enum_name
					sql_drop_enum='DROP TYPE IF EXISTS '+column_type+' CASCADE;'
					sql_create_enum="CREATE TYPE "+column_type+" AS ENUM ("+alter_dic["dimension"]+");"
					ddl_enum.append(sql_drop_enum)
					ddl_enum.append(sql_create_enum)
				if column_type=="character varying" or column_type=="character" or column_type=='numeric' or column_type=='bit' or column_type=='float':
						column_type=column_type+"("+str(alter_dic["dimension"])+")"
				query = ' '.join(ddl_enum) + """ALTER TABLE "%s" ALTER COLUMN "%s" SET DATA TYPE %s USING "%s"::%s ;""" % (table_name, column_name, column_type, column_name, column_type)
				return query
		query = ' '.join(ddl_enum)+" "+query_cmd + ' '+ table_name+ ' ' +', '.join(alter_cmd)+" ;"
		return query

	
					

	def drop_primary_key(self, token):
		self.logger.info("dropping primary key for table %s" % (token["name"],))
		sql_gen="""
						SELECT  DISTINCT
							format('ALTER TABLE %%I.%%I DROP CONSTRAINT %%I;',
							table_schema,
							table_name,
							constraint_name
							)
						FROM 
							information_schema.key_column_usage 
						WHERE 
								table_schema=%s 
							AND table_name=%s;
					"""
		self.pg_conn.pgsql_cur.execute(sql_gen, (self.pg_conn.dest_schema, token["name"]))
		value_check=self.pg_conn.pgsql_cur.fetchone()
		if value_check:
			sql_drop=value_check[0]
			self.pg_conn.pgsql_cur.execute(sql_drop)
			self.unregister_table(token["name"])

	def gen_query(self, token):
		""" the function generates the ddl"""
		query=""
		
		if token["command"] =="DROP TABLE":
			query=" %(command)s IF EXISTS \"%(name)s\" CASCADE;" % token
		elif token["command"] =="TRUNCATE":
			query=" %(command)s TABLE \"%(name)s\" CASCADE;" % token
		elif token["command"] =="CREATE TABLE":
			table_metadata={}
			table_metadata["columns"]=token["columns"]
			table_metadata["name"]=token["name"]
			table_metadata["indices"]=token["indices"]
			self.table_metadata={}
			self.table_metadata[token["name"]]=table_metadata
			self.build_tab_ddl()
			self.build_idx_ddl()
			query_type=' '.join(self.type_ddl[token["name"]])
			query_table=self.table_ddl[token["name"]]
			query_idx=' '.join(self.idx_ddl[token["name"]])
			query=query_type+query_table+query_idx
			self.store_table(token["name"])
		elif token["command"] == "ALTER TABLE":
			query=self.build_alter_table(token)
		elif token["command"] == "DROP PRIMARY KEY":
			self.drop_primary_key(token)
		return query 
		
		
	def write_ddl(self, token, query_data, obflist):
		path_clear=""" SET search_path="%s"; """ % (self.dest_schema, )
		path_obf= """ SET search_path="%s"; """ % (self.obf_schema, ) 
		ddl_query=self.gen_query(token)
		if token["command"] ==  "ALTER TABLE" and token["name"] not in obflist and self.dest_schema != self.obf_schema:
			sql_drop_view = """DROP VIEW IF EXISTS "%s" CASCADE; """ % (token["name"], )
			sql_create_view = """CREATE OR REPLACE VIEW "%s" AS SELECT * FROM "%s"."%s";""" % (token["name"],self.dest_schema, token["name"] )
			pg_ddl = path_obf + sql_drop_view + path_clear + ddl_query + path_obf + sql_create_view
		else:
			pg_ddl=path_clear+ddl_query
		log_table=query_data["log_table"]
		insert_vals=(	query_data["batch_id"], 
								token["name"],  
								query_data["schema"], 
								query_data["binlog"], 
								query_data["logpos"], 
								pg_ddl
							)
		sql_insert="""
								INSERT INTO sch_ninja."""+log_table+"""
								(
									i_id_batch, 
									v_table_name, 
									v_schema_name, 
									enm_binlog_event, 
									t_binlog_name, 
									i_binlog_position, 
									t_query
								)
								VALUES
								(
									%s,
									%s,
									%s,
									'ddl',
									%s,
									%s,
									%s
								)
						"""
		self.pg_conn.pgsql_cur.execute(sql_insert, insert_vals)
		
	def truncate_table(self, table_name, schema_name):
		sql_clean="""
							SELECT 
								format('SET lock_timeout=''120s'';TRUNCATE TABLE %%I.%%I;',schemaname,tablename) v_truncate,
								format('DELETE FROM %%I.%%I;',schemaname,tablename) v_delete,
								format('VACUUM %%I.%%I;',schemaname,tablename) v_vacuum,
								format('%%I.%%I',schemaname,tablename) as v_tab,
								tablename    
							FROM 
								pg_tables
							WHERE
								tablename=%s
								AND schemaname=%s
						"""
		self.pg_conn.pgsql_cur.execute(sql_clean, (table_name, schema_name))
		tab_clean=self.pg_conn.pgsql_cur.fetchone()
		if  tab_clean:
			st_truncate=tab_clean[0]
			st_delete=tab_clean[1]
			st_vacuum=tab_clean[2]
			tab_name=tab_clean[3]
			try:
				self.logger.debug("running truncate table on %s" % (tab_name,))
				self.pg_conn.pgsql_cur.execute(st_truncate)
				
			except:
				self.logger.info("truncate failed, fallback to delete on table %s" % (tab_name,))
				self.pg_conn.pgsql_cur.execute(st_delete)
				self.logger.info("running vacuum on table %s" % (tab_name,))
				self.pg_conn.pgsql_cur.execute(st_vacuum)
			return True
		else:
			return False
	
	def truncate_tables(self):
		table_limit = ''
		if self.table_limit[0] != '*':
			table_limit = self.pg_conn.pgsql_cur.mogrify("""WHERE v_table IN  (SELECT unnest(%s))""",(self.table_limit, )).decode()
		
		
		sql_clean=""" 
						SELECT DISTINCT
							format('SET lock_timeout=''120s'';TRUNCATE TABLE %%I.%%I;',v_schema,v_table) v_truncate,
							format('DELETE FROM %%I.%%I;',v_schema,v_table) v_delete,
							format('VACUUM %%I.%%I;',v_schema,v_table) v_vacuum,
							format('%%I.%%I',v_schema,v_table) as v_tab,
							v_table
						FROM
							sch_ninja.t_index_def 
						%s
						
						ORDER BY 
							v_table
		""" % (table_limit, )
		self.pg_conn.pgsql_cur.execute(sql_clean)
		tab_clean=self.pg_conn.pgsql_cur.fetchall()
		for stat_clean in tab_clean:
			st_truncate=stat_clean[0]
			st_delete=stat_clean[1]
			st_vacuum=stat_clean[2]
			tab_name=stat_clean[3]
			try:
				self.logger.info("truncating table %s" % (tab_name,))
				self.pg_conn.pgsql_cur.execute(st_truncate)
				
			except:
				self.logger.info("truncate failed, fallback to delete on table %s" % (tab_name,))
				self.pg_conn.pgsql_cur.execute(st_delete)
				self.logger.info("running vacuum on table %s" % (tab_name,))
				self.pg_conn.pgsql_cur.execute(st_vacuum)

	def get_index_def(self):
		table_limit = ''
		if self.table_limit[0] != '*':
			table_limit = self.pg_conn.pgsql_cur.mogrify("""WHERE table_name IN  (SELECT unnest(%s))""",(self.table_limit, )).decode()
		
		drp_msg = 'Do you want to clean the existing index definitions in t_index_def?.\n YES/No\n' 
		if sys.version_info[0] == 3:
			drop_idx = input(drp_msg)
		else:
			drop_idx = raw_input(drp_msg)
		if drop_idx == 'YES':
			sql_delete = """ DELETE FROM sch_ninja.t_index_def;"""
			self.pg_conn.pgsql_cur.execute(sql_delete)
		elif drop_idx in self.lst_yes or len(drop_idx) == 0:
			print('Please type YES all uppercase to confirm')
			sys.exit()
		self.logger.info("collecting indices and pk for schema %s" % (self.pg_conn.dest_schema,))
		
		sql_get_idx=""" 
				
				INSERT INTO sch_ninja.t_index_def
					(
						v_schema,
						v_table,
						v_index,
						t_create,
						t_drop
					)
				SELECT 
					schema_name,
					table_name,
					index_name,
					CASE
						WHEN indisprimary
						THEN
							format('ALTER TABLE %%I.%%I ADD CONSTRAINT %%I %%s',
								schema_name,
								table_name,
								index_name,
								pg_get_constraintdef(const_id)
							)
							
						ELSE
							pg_get_indexdef(index_id)    
					END AS t_create,
					CASE
						WHEN indisprimary
						THEN
							format('ALTER TABLE %%I.%%I DROP CONSTRAINT %%I CASCADE',
								schema_name,
								table_name,
								index_name
								
							)
							
						ELSE
							format('DROP INDEX %%I.%%I',
								schema_name,
								index_name
								
							)
					END AS  t_drop
					
				FROM

				(
				SELECT 
					tab.relname AS table_name,
					indx.relname AS index_name,
					idx.indexrelid index_id,
					indisprimary,
					sch.nspname schema_name,
					cns.oid as const_id
					
				FROM
					pg_index idx
					INNER JOIN pg_class indx
					ON
						idx.indexrelid=indx.oid
					INNER JOIN pg_class tab
					INNER JOIN pg_namespace sch
					ON 
						tab.relnamespace=sch.oid
					
					ON
						idx.indrelid=tab.oid
					LEFT OUTER JOIN pg_constraint cns
					ON 
							indx.relname=cns.conname
						AND cns.connamespace=sch.oid
					
				WHERE
					sch.nspname=%s
				) idx
		""" + table_limit +""" ON CONFLICT DO NOTHING"""
		self.pg_conn.pgsql_cur.execute(sql_get_idx, (self.pg_conn.dest_schema, ))
		
	
	def drop_src_indices(self):
		table_limit = ''
		if self.table_limit[0] != '*':
			table_limit = self.pg_conn.pgsql_cur.mogrify("""WHERE v_table IN  (SELECT unnest(%s))""",(self.table_limit, )).decode()
		
		sql_idx="""SELECT t_drop FROM  sch_ninja.t_index_def %s; """ % (table_limit, )
		self.pg_conn.pgsql_cur.execute(sql_idx)
		idx_drop=self.pg_conn.pgsql_cur.fetchall()
		for drop_stat in idx_drop:
			self.pg_conn.pgsql_cur.execute(drop_stat[0])
			
	def create_src_indices(self):
		table_limit = ''
		if self.table_limit[0] != '*':
			table_limit = self.pg_conn.pgsql_cur.mogrify("""WHERE v_table IN  (SELECT unnest(%s))""",(self.table_limit, )).decode()
		
		sql_idx="""SELECT t_create FROM  sch_ninja.t_index_def %s;""" % (table_limit, )
		self.pg_conn.pgsql_cur.execute(sql_idx)
		idx_create=self.pg_conn.pgsql_cur.fetchall()
		for create_stat in idx_create:
			self.pg_conn.pgsql_cur.execute(create_stat[0])
	
	def add_source(self, source_name, schema_clear, schema_obf):
		"""
			The method add a new source in the replica catalogue. 
			If the source name is already present an error message is emitted without further actions.
			:param source_name: The source name stored in the configuration parameter source_name.
			:param schema_clear: The schema with the data in clear.
			:param schema_obf: The schema with the data obfuscated.
		"""
		sql_source = """
					SELECT 
						count(i_id_source)
					FROM 
						sch_ninja.t_sources 
					WHERE 
						t_source=%s
				;
			"""
		self.pg_conn.pgsql_cur.execute(sql_source, (source_name, ))
		source_data = self.pg_conn.pgsql_cur.fetchone()
		cnt_source = source_data[0]
		if cnt_source == 0:
			sql_add = """
				INSERT INTO sch_ninja.t_sources 
					( 
						t_source,
						t_dest_schema,
						t_obf_schema
					) 
				VALUES 
					(
						%s,
						%s,
						%s
					)
				RETURNING 
					i_id_source
			; """
			self.pg_conn.pgsql_cur.execute(sql_add, (source_name, schema_clear, schema_obf ))
			source_add = self.pg_conn.pgsql_cur.fetchone()
			sql_update = """
				UPDATE sch_ninja.t_sources
					SET v_log_table=ARRAY[
						't_log_replica_1_src_%s',
						't_log_replica_2_src_%s'
					]
				WHERE i_id_source=%s
				;
			"""
			self.pg_conn.pgsql_cur.execute(sql_update,  (source_add[0],source_add[0], source_add[0] ))
			
			sql_parts = """SELECT sch_ninja.fn_refresh_parts() ;"""
			self.pg_conn.pgsql_cur.execute(sql_parts)
		else:
			print("Source %s already registered." % source_name)
		sys.exit()
		
	def drop_source(self, source_name):
		"""
			Drops the source from the replication catalogue discarding any replica reference.
			:param source_name: The source name stored in the configuration parameter source_name.
		"""
		sql_delete = """ DELETE FROM sch_ninja.t_sources 
					WHERE  t_source=%s
					RETURNING v_log_table
					; """
		self.pg_conn.pgsql_cur.execute(sql_delete, (source_name, ))
		source_drop = self.pg_conn.pgsql_cur.fetchone()
		for log_table in source_drop[0]:
			sql_drop = """DROP TABLE sch_ninja."%s"; """ % (log_table)
			self.pg_conn.pgsql_cur.execute(sql_drop)
	
	def get_source_status(self, source_name):
		sql_source = """
					SELECT 
						enm_status
					FROM 
						sch_ninja.t_sources 
					WHERE 
						t_source=%s
				;
			"""
		self.pg_conn.pgsql_cur.execute(sql_source, (source_name, ))
		source_data = self.pg_conn.pgsql_cur.fetchone()
		if source_data:
			source_status = source_data[0]
		else:
			source_status = 'Not registered'
		return source_status
		

		
	def get_status(self):
		"""
			The metod lists the sources with the running status and the eventual lag 
			
			:return: psycopg2 fetchall results 
			:rtype: psycopg2 tuple
		"""
		sql_status="""
			SELECT
				t_source,
				t_dest_schema,
				enm_status,
				 date_trunc('seconds',now())-ts_last_received lag,
				ts_last_received,
				ts_last_received-ts_last_replay,
				ts_last_replay,
				t_obf_schema
			FROM 
				sch_ninja.t_sources
			ORDER BY 
				t_source
			;
		"""
		self.pg_conn.pgsql_cur.execute(sql_status)
		results = self.pg_conn.pgsql_cur.fetchall()
		return results
		
	def set_source_id(self, source_status):
		sql_source = """
					UPDATE sch_ninja.t_sources
					SET
						enm_status=%s
					WHERE
						t_source=%s
					RETURNING i_id_source,t_dest_schema,t_obf_schema
				;
			"""
		source_name=self.pg_conn.global_conf.source_name
		self.pg_conn.pgsql_cur.execute(sql_source, (source_status, source_name))
		source_data=self.pg_conn.pgsql_cur.fetchone()
		try:
			self.i_id_source=source_data[0]
			self.dest_schema=source_data[1]
			self.obf_schema=source_data[2]
		except:
			print("Source %s is not registered." % source_name)
			sys.exit()
	
			
	def clean_batch_data(self):
		sql_delete="""DELETE FROM sch_ninja.t_replica_batch 
								WHERE i_id_source=%s;
							"""
		self.pg_conn.pgsql_cur.execute(sql_delete, (self.i_id_source, ))
	
	def check_primary_key(self,table_to_add):
		sql_check = """
			SELECT  
				tab.relname
			FROM
				pg_class tab
				INNER JOIN pg_namespace sch
					ON tab.relnamespace = sch.oid 
				INNER JOIN pg_constraint  pk
					ON tab.oid = pk.conrelid
			WHERE
					pk.contype = 'p'
				AND	sch.nspname = %s
				AND	tab.relname  = ANY(%s)
		"""
		self.pg_conn.pgsql_cur.execute(sql_check, (self.dest_schema, table_to_add ))
		tables_pk = self.pg_conn.pgsql_cur.fetchall()
		return tables_pk
		
	def get_inconsistent_tables(self):
		"""
			The method collects the tables in not consistent state.
			The informations are stored in a dictionary which key is the table's name.
			The dictionary is used in the read replica loop to determine wheter the table's modifications
			should be ignored because in not consistent state.
			
			:return: a dictionary with the tables in inconsistent state and their snapshot coordinates.
			:rtype: dictionary
		"""
		sql_get = """
			SELECT
				v_schema_name,				
				v_table_name,
				t_binlog_name,
				i_binlog_position
			FROM
				sch_ninja.t_replica_tables
			WHERE
				t_binlog_name IS NOT NULL
				AND i_binlog_position IS NOT NULL
				AND i_id_source = %s
		;
		"""
		inc_dic = {}
		self.pg_conn.pgsql_cur.execute(sql_get, (self.i_id_source, ))
		inc_results = self.pg_conn.pgsql_cur.fetchall()
		for table  in inc_results:
			tab_dic = {}
			tab_dic["schema"]  = table[0]
			tab_dic["table"]  = table[1]
			tab_dic["log_seq"]  = int(table[2].split('.')[1])
			tab_dic["log_pos"]  = int(table[3])
			inc_dic[table[1]] = tab_dic
		return inc_dic
		
	def set_consistent_table(self, table):
		"""
			The method set to NULL the  binlog name and position for the given table.
			When the table is marked consistent the read replica loop reads and saves the table's row images.
			
			:param table: the table name
		"""
		sql_set = """
			UPDATE sch_ninja.t_replica_tables
				SET 
					t_binlog_name = NULL,
					i_binlog_position = NULL
			WHERE
					i_id_source = %s
				AND	v_table_name = %s
				AND	v_schema_name = %s
			;
		"""
		self.pg_conn.pgsql_cur.execute(sql_set, (self.i_id_source, table, self.dest_schema))
		self.pg_conn.pgsql_cur.execute(sql_set, (self.i_id_source, table, self.obf_schema))
	
	def delete_table_events(self):
		"""
			The method removes the events from the log table for specific table and source. 
			Is used to cleanup any residual event for a a synced table in the replica_engine's sync_table method.
		"""
		sql_clean = """
			DELETE FROM sch_ninja.t_log_replica
			WHERE 
				i_id_event IN (
							SELECT 
								log.i_id_event
							FROM
								sch_ninja.t_replica_batch bat
								INNER JOIN sch_ninja.t_log_replica log
									ON  log.i_id_batch=bat.i_id_batch
							WHERE
									log.v_table_name=ANY(%s)
								AND 	bat.i_id_source=%s
						)
			;
		"""
		self.pg_conn.pgsql_cur.execute(sql_clean, (self.table_limit, self.i_id_source, ))
		
