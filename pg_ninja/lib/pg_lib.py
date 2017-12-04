import io
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
import sys
import json
import datetime
import decimal
import time
import binascii
import os
from distutils.sysconfig import get_python_lib
import multiprocessing as mp


class pg_encoder(json.JSONEncoder):
	def default(self, obj):
		if 		isinstance(obj, datetime.time) or \
				isinstance(obj, datetime.datetime) or  \
				isinstance(obj, datetime.date) or \
				isinstance(obj, decimal.Decimal) or \
				isinstance(obj, datetime.timedelta) or \
				isinstance(obj, set):
					
			return str(obj)
		return json.JSONEncoder.default(self, obj)
		
class pgsql_source(object):
	def __init__(self):
		"""
			Class constructor, the method sets the class variables and configure the
			operating parameters from the args provided t the class.
		"""
		self.schema_tables = {}
		self.schema_mappings = {}
		self.schema_loading = {}
		self.schema_list = []
		self.schema_only = {}
	
	def __del__(self):
		"""
			Class destructor, tries to disconnect the postgresql connection.
		"""
		pass
	
	def __init_obfuscation(self):
		"""
			The method initialises the obfuscation into the obfuscated loading schema. 
			No swap is performed in this method though.
		"""
		self.logger.info("building obfuscation for source %s" % self.source)
		for schema in self.obfuscate_schemas:
			try:
				clear_tables = [table for table in self.schema_tables[schema] if table not in self.obfuscation[schema]]
				destination_schema = self.schema_loading[schema]["loading_obfuscated"]
				self.logger.info("processing schema %s into %s" % (schema, destination_schema))
				for table in self.obfuscation[schema]:
					try:
						table_obfuscation = self.obfuscation[schema][table]
						self.logger.info("Creating the table %s.%s " % (destination_schema, table, ))
						self.pg_engine.create_obfuscated_table(table,  schema)
						self.pg_engine.copy_obfuscated_table(table,  schema, table_obfuscation)
						self.pg_engine.create_obfuscated_indices(table,  schema)
						
					except:
						self.logger.error("Could not obfuscate the table  %s.%s " % (destination_schema,  table))
					
				for table in clear_tables:
					self.pg_engine.create_clear_view(schema, table)
			except KeyError:
				self.logger.warning("the schema %s doesn't exists" % (schema))


	def __set_copy_max_memory(self):
		"""
			The method sets the class variable self.copy_max_memory using the value stored in the 
			source setting.

		"""
		copy_max_memory = str(self.source_config["copy_max_memory"])[:-1]
		copy_scale = str(self.source_config["copy_max_memory"])[-1]
		try:
			int(copy_scale)
			copy_max_memory = self.source_config["copy_max_memory"]
		except:
			if copy_scale =='k':
				copy_max_memory = str(int(copy_max_memory)*1024)
			elif copy_scale =='M':
				copy_max_memory = str(int(copy_max_memory)*1024*1024)
			elif copy_scale =='G':
				copy_max_memory = str(int(copy_max_memory)*1024*1024*1024)
			else:
				print("**FATAL - invalid suffix in parameter copy_max_memory  (accepted values are (k)ilobytes, (M)egabytes, (G)igabytes.")
				sys.exit(3)
		self.copy_max_memory = copy_max_memory


	def __init_sync(self):
		"""
			The method calls the common steps required to initialise the database connections and
			class attributes within sync_tables,refresh_schema and init_replica.
		"""
		self.source_config = self.sources[self.source]
		self.out_dir = self.source_config["out_dir"]
		self.copy_mode = self.source_config["copy_mode"]
		self.pg_engine.lock_timeout = self.source_config["lock_timeout"]
		self.pg_engine.grant_select_to = self.source_config["grant_select_to"]
		self.source_conn = self.source_config["db_conn"]
		self.__set_copy_max_memory()
		db_object = self.__connect_db( auto_commit=True, dict_cursor=True)
		self.pgsql_conn = db_object["connection"]
		self.pgsql_cursor = db_object["cursor"]
		self.pg_engine.connect_db()
		self.schema_mappings = self.pg_engine.get_schema_mappings()
		self.pg_engine.schema_tables = self.schema_tables
		if self.obfuscation:
			self.obfuscate_schemas = [schema for schema in self.obfuscation]
		

	
	def __connect_db(self, auto_commit=True, dict_cursor=False):
		"""
			Connects to PostgreSQL using the parameters stored in self.dest_conn. The dictionary is built using the parameters set via adding the key dbname to the self.pg_conn dictionary.
			This method's connection and cursors are widely used in the procedure except for the replay process which uses a 
			dedicated connection and cursor.
			
			:return: a dictionary with the objects connection and cursor 
			:rtype: dictionary
		"""
		if self.source_conn:
			strconn = "dbname=%(database)s user=%(user)s host=%(host)s password=%(password)s port=%(port)s connect_timeout=%(connect_timeout)s"  % self.source_conn
			pgsql_conn = psycopg2.connect(strconn)
			pgsql_conn .set_client_encoding(self.source_conn["charset"])
			if dict_cursor:
				pgsql_cur = pgsql_conn .cursor(cursor_factory=RealDictCursor)
			else:
				pgsql_cur = pgsql_conn .cursor()
			self.logger.debug("Changing the autocommit flag to %s" % auto_commit)
			pgsql_conn.set_session(autocommit=auto_commit)

		elif not self.source_conn:
			self.logger.error("Undefined database connection string. Exiting now.")
			sys.exit()
		
		return {'connection': pgsql_conn, 'cursor': pgsql_cur }
	
	def __export_snapshot(self, queue):
		"""
			The method exports a database snapshot and stays idle in transaction until a message from the parent
			process tell it to exit.
			The method stores the snapshot id in the queue for the parent's usage.
			
			:param queue: the queue object used to exchange messages between the parent and the child
		"""
		self.logger.debug("exporting database snapshot for source %s" % self.source)
		sql_snap = """
			BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
			SELECT pg_export_snapshot();
		"""
		db_snap = self.__connect_db(False)
		db_conn = db_snap["connection"]
		db_cursor = db_snap["cursor"]
		db_cursor.execute(sql_snap)
		snapshot_id = db_cursor.fetchone()[0]
		queue.put(snapshot_id)
		continue_loop = True
		while continue_loop:
			continue_loop = queue.get()
			time.sleep(5)
		db_conn.commit()
		
	def __build_table_exceptions(self):
		"""
			The method builds two dictionaries from the limit_tables and skip tables values set for the source.
			The dictionaries are intended to be used in the get_table_list to cleanup the list of tables per schema.
			The method manages the particular case of when the class variable self.tables is set.
			In that case only the specified tables in self.tables will be synced. Should limit_tables be already 
			set, then the resulting list is the intersection of self.tables and limit_tables.
		"""
		self.limit_tables = {}
		self.skip_tables = {}
		limit_tables = self.source_config["limit_tables"]
		skip_tables = self.source_config["skip_tables"]
		
		if self.tables !='*':
			tables = [table.strip() for table in self.tables.split(',')]
			if limit_tables:
				limit_tables = [table for table in tables if table in limit_tables]
			else:
				limit_tables = tables
			self.schema_only = {table.split('.')[0] for table in limit_tables}
			
		
		if limit_tables:
			table_limit = [table.split('.') for table in limit_tables]
			for table_list in table_limit:
				list_exclude = []
				try:
					list_exclude = self.limit_tables[table_list[0]] 
					list_exclude.append(table_list[1])
				except KeyError:
					list_exclude.append(table_list[1])
				self.limit_tables[table_list[0]]  = list_exclude
		if skip_tables:
			table_skip = [table.split('.') for table in skip_tables]		
			for table_list in table_skip:
				list_exclude = []
				try:
					list_exclude = self.skip_tables[table_list[0]] 
					list_exclude.append(table_list[1])
				except KeyError:
					list_exclude.append(table_list[1])
				self.skip_tables[table_list[0]]  = list_exclude
		


	def __get_table_list(self):
		"""
			The method pulls the table list from the information_schema. 
			The list is stored in a dictionary  which key is the table's schema.
		"""
		sql_tables="""
			SELECT 
				table_name
			FROM 
				information_schema.TABLES 
			WHERE 
					table_type='BASE TABLE' 
				AND table_schema=%s
			;
		"""
		for schema in self.schema_list:
			self.pgsql_cursor.execute(sql_tables, (schema, ))
			table_list = [table["table_name"] for table in self.pgsql_cursor.fetchall()]
			try:
				limit_tables = self.limit_tables[schema]
				if len(limit_tables) > 0:
					table_list = [table for table in table_list if table in limit_tables]
			except KeyError:
				pass
			try:
				skip_tables = self.skip_tables[schema]
				if len(skip_tables) > 0:
					table_list = [table for table in table_list if table not in skip_tables]
			except KeyError:
				pass
			
			self.schema_tables[schema] = table_list
	
	def __create_destination_schemas(self):
		"""
			Creates the loading schemas in the destination database and associated tables listed in the dictionary
			self.schema_tables.
			The method builds a dictionary which associates the destination schema to the loading schema. 
			The loading_schema is named after the destination schema plus with the prefix _ and the _tmp suffix.
			As postgresql allows, by default up to 64  characters for an identifier, the original schema is truncated to 59 characters,
			in order to fit the maximum identifier's length.
			The mappings are stored in the class dictionary schema_loading.
		"""
		for schema in self.schema_list:
			destination_schema = self.schema_mappings[schema]["clear"]
			obfuscated_schema = self.schema_mappings[schema]["obfuscate"]
			loading_schema = "_%s_tmp" % destination_schema[0:59]
			loading_obfuscated = "_%s_tmp" % obfuscated_schema[0:59]
			self.schema_loading[schema] = {'destination':destination_schema, 'loading':loading_schema, 'obfuscated': obfuscated_schema, 'loading_obfuscated': loading_obfuscated}
			self.logger.debug("Creating the loading schema %s." % loading_schema)
			self.pg_engine.create_database_schema(loading_schema)
			self.logger.debug("Creating the destination schema %s." % destination_schema)
			self.pg_engine.create_database_schema(destination_schema)
			self.logger.debug("Creating the obfuscated schema %s." % obfuscated_schema)
			self.pg_engine.create_database_schema(obfuscated_schema)
			self.logger.debug("Creating the loading obfuscated schema %s." % loading_obfuscated)
			self.pg_engine.create_database_schema(loading_obfuscated)

	def __get_table_metadata(self, table, schema):
		"""
			The method builds the table's metadata querying the information_schema.
			The data is returned as a dictionary.
			
			:param table: The table name
			:param schema: The table's schema
			:return: table's metadata as a cursor dictionary
			:rtype: dictionary
		"""
		sql_metadata="""
			SELECT
				col.attname as column_name,
				(
					SELECT 
						pg_catalog.pg_get_expr(def.adbin, def.adrelid)
					FROM 
						pg_catalog.pg_attrdef def
					WHERE 
							def.adrelid = col.attrelid 
						AND def.adnum = col.attnum 
						AND col.atthasdef
				) as column_default,
				col.attnum as ordinal_position,
				CASE 
					WHEN typ.typcategory ='E'
					THEN 
						'enum'
					WHEN typ.typcategory='C'
					THEN
						'composite' 
					
				ELSE
					pg_catalog.format_type(col.atttypid, col.atttypmod)
				END
				AS type_format,
				(
					SELECT 
						pg_get_serial_sequence(format('%%I.%%I',tabsch.nspname,tab.relname), col.attname) IS NOT NULL
					FROM 
						pg_catalog.pg_class tab
						INNER JOIN pg_catalog.pg_namespace tabsch
						ON	tab.relnamespace=tabsch.oid
					WHERE
						tab.oid=col.attrelid
				) as col_serial,
				typ.typcategory as type_category,
				CASE
					WHEN typ.typcategory='E'
					THEN
					(
						SELECT 
							string_agg(quote_literal(enumlabel),',') 
						FROM 
							pg_catalog.pg_enum enm 
						WHERE enm.enumtypid=typ.oid 
					)
					WHEN typ.typcategory='C'
					THEN 
					(
						SELECT 
							string_agg(
								format('%%I %%s',
									attname,
									pg_catalog.format_type(atttypid, atttypmod)
								)
							,
							','
							)	 
						FROM 
							pg_catalog.pg_attribute 
						WHERE 
							attrelid=format(
								'%%I.%%I',
								sch.nspname,
								typ.typname)::regclass
							)
				END AS typ_elements,
				col.attnotnull as not_null
			FROM 
				pg_catalog.pg_attribute col
				INNER JOIN pg_catalog.pg_type typ
					ON  col.atttypid=typ.oid
				INNER JOIN pg_catalog.pg_namespace sch
					ON typ.typnamespace=sch.oid
			WHERE 
					col.attrelid = %s::regclass 
				AND NOT col.attisdropped
				AND col.attnum>0
			ORDER BY 
				col.attnum
			;
			;
		"""
		tab_regclass = '"%s"."%s"' % (schema, table)
		self.pgsql_cursor.execute(sql_metadata, (tab_regclass, ))
		table_metadata=self.pgsql_cursor.fetchall()
		return table_metadata


	def __create_destination_tables(self):
		"""
			The method creates the destination tables in the loading schema.
			The tables names are looped using the values stored in the class dictionary schema_tables.
		"""
		for schema in self.schema_tables:
			table_list = self.schema_tables[schema]
			for table in table_list:
				table_metadata = self.__get_table_metadata(table, schema)
				self.pg_engine.create_table(table_metadata, table, schema, 'pgsql')
	
	def __drop_loading_schemas(self):
		"""
			The method drops the loading schemas from the destination database.
			The drop is performed on the schemas generated in create_destination_schemas. 
			The method assumes the class dictionary schema_loading is correctly set.
		"""
		for schema in self.schema_loading:
			loading_schema = self.schema_loading[schema]["loading"]
			loading_obfuscated = self.schema_loading[schema]["loading_obfuscated"]
			self.logger.debug("Dropping the loading clear schema %s." % loading_schema)
			self.pg_engine.drop_database_schema(loading_schema, True)
			self.logger.debug("Dropping the obfuscated obfuscated schema %s." % loading_obfuscated)
			self.pg_engine.drop_database_schema(loading_obfuscated, True)
	
	def __copy_data(self, schema, table, db_copy):
		
		sql_snap = """
			BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
			SET TRANSACTION SNAPSHOT %s;
		"""
		out_file = '%s/%s_%s.csv' % (self.out_dir, schema, table )
		loading_schema = self.schema_loading[schema]["loading"]
		from_table = '"%s"."%s"' % (schema, table)
		to_table = '"%s"."%s"' % (loading_schema, table)
		
		db_conn = db_copy["connection"]
		db_cursor = db_copy["cursor"]
		
		db_cursor.execute(sql_snap, (self.snapshot_id, ))
		self.logger.debug("exporting table %s.%s in %s" % (schema , table,  out_file))
		copy_file = open(out_file, 'wb')
		db_cursor.copy_to(copy_file, from_table)
		
		copy_file.close()
		self.logger.debug("loading the file %s in table %s.%s " % (out_file,  loading_schema , table,  ))
		
		copy_file = open(out_file, 'rb')
		self.pg_engine.pgsql_cur.copy_from(copy_file, to_table)
		copy_file.close()
		db_conn.commit()
		try:
			remove(out_file)
		except:
			pass
		
	
	
	def __create_indices(self):
		"""
			The method loops over the tables, queries the origin's database and creates the same indices
			on the loading schema.
		"""
		db_copy = self.__connect_db(False)
		db_conn = db_copy["connection"]
		db_cursor = db_copy["cursor"]
		sql_get_idx = """
			SELECT 
				CASE
					WHEN con.conname IS NOT NULL
					THEN
						format('ALTER TABLE %%I ADD CONSTRAINT %%I %%s ;',tab.relname,con.conname,pg_get_constraintdef(con.oid))
					ELSE
						format('%%s ;',regexp_replace(pg_get_indexdef(idx.oid), '("?\w+"?\.)', ''))
				END AS ddl_text,
				CASE
					WHEN con.conname IS NOT NULL
					THEN
						format('Adding primary key to table %%I',tab.relname)
					ELSE
						format('Adding index %%I to table %%I',idx.relname,tab.relname)
				END AS ddl_msg
			FROM

				pg_class tab 
				INNER JOIN pg_namespace sch
				ON	
					sch.oid=tab.relnamespace
				INNER JOIN pg_index ind
				ON
					ind.indrelid=tab.oid
				INNER JOIN pg_class idx
				ON
					ind.indexrelid=idx.oid
				LEFT OUTER JOIN pg_constraint con
				ON
						con.conrelid=tab.oid
					AND	idx.oid=con.conindid
				
			WHERE
				(		
						contype='p' 
					OR 	contype IS NULL
				)
				AND	tab.relname=%s
				AND	sch.nspname=%s
			;
		"""
		
		for schema in self.schema_tables:
			table_list = self.schema_tables[schema]
			for table in table_list:
				loading_schema = self.schema_loading[schema]["loading"]
				self.pg_engine.pgsql_cur.execute('SET search_path=%s;', (loading_schema, ))
				db_cursor.execute(sql_get_idx, (table, schema))
				idx_tab = db_cursor.fetchall()
				for idx in idx_tab:
					self.logger.info(idx[1])
					try:
						self.pg_engine.pgsql_cur.execute(idx[0])
					except:
						self.logger.error("an error occcurred when executing %s" %(idx[0]))
					
		
		db_conn.close()
		
	def __copy_tables(self):
		"""
			The method copies the data between tables, from the postgres source and the corresponding
			postgresql loading schema. Before the process starts a snapshot is exported in order to get
			a consistent database copy at the time of the snapshot.
		"""
		
		queue = mp.Queue()
		snap_exp = mp.Process(target=self.__export_snapshot, args=(queue,), name='snap_export',daemon=True)
		snap_exp.start()
		self.snapshot_id = queue.get()
		db_copy = self.__connect_db(False)
		
		for schema in self.schema_tables:
			table_list = self.schema_tables[schema]
			for table in table_list:
				self.__copy_data(schema, table, db_copy)
		queue.put(False)
		db_copy["connection"].close()
	
	def init_replica(self):
		"""
			The method performs a full init replica for the given source
		"""
		self.logger.debug("starting init replica for source %s" % self.source)
		self.__init_sync()
		self.schema_list = [schema for schema in self.schema_mappings]
		self.__build_table_exceptions()
		self.__get_table_list()
		self.__create_destination_schemas()
		self.pg_engine.schema_loading = self.schema_loading
		self.pg_engine.set_source_status("initialising")
		try:
			self.__create_destination_tables()
			self.__copy_tables()
			self.__create_indices()
			if self.obfuscation:
				self.__init_obfuscation()
			self.pg_engine.grant_select()
			self.pg_engine.swap_schemas()
			self.__drop_loading_schemas()
			self.pg_engine.set_source_status("initialised")
		except:
			self.__drop_loading_schemas()
			self.pg_engine.set_source_status("error")
			raise
		

class pg_engine(object):
	def __init__(self):
		python_lib=get_python_lib()
		self.sql_dir = "%s/pg_ninja/sql/" % python_lib
		self.table_ddl={}
		self.idx_ddl={}
		self.type_ddl={}
		self.idx_sequence=0
		self.type_dictionary = {
			'integer':'integer',
			'mediumint':'bigint',
			'tinyint':'integer',
			'smallint':'integer',
			'int':'integer',
			'bigint':'bigint',
			'varchar':'character varying',
			'character varying':'character varying',
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
		self.dest_conn = None
		self.pgsql_conn = None
		self.logger = None
		self.idx_sequence = 0
		self.lock_timeout = 0
		
	def __del__(self):
		"""
			Class destructor, tries to disconnect the postgresql connection.
		"""
		self.disconnect_db()
	
	def set_autocommit_db(self, auto_commit):
		"""
			The method sets the auto_commit flag for the class connection self.pgsql_conn.
			In general the connection is always autocommit but in some operations (e.g. update_schema_mappings) 
			is better to run the process in a single transaction in order to avoid inconsistencies.
			
			:param autocommit: boolean flag which sets autocommit on or off.

		"""
		self.logger.debug("Changing the autocommit flag to %s" % auto_commit)
		self.pgsql_conn.set_session(autocommit=auto_commit)

	
	def connect_db(self):
		"""
			Connects to PostgreSQL using the parameters stored in self.dest_conn. The dictionary is built using the parameters set via adding the key dbname to the self.pg_conn dictionary.
			This method's connection and cursors are widely used in the procedure except for the replay process which uses a 
			dedicated connection and cursor.
		"""
		if self.dest_conn and not self.pgsql_conn:
			strconn = "dbname=%(database)s user=%(user)s host=%(host)s password=%(password)s port=%(port)s"  % self.dest_conn
			self.pgsql_conn = psycopg2.connect(strconn)
			self.pgsql_conn .set_client_encoding(self.dest_conn["charset"])
			self.set_autocommit_db(True)
			self.pgsql_cur = self.pgsql_conn .cursor()
		elif not self.dest_conn:
			self.logger.error("Undefined database connection string. Exiting now.")
			sys.exit()
		elif self.pgsql_conn:
			self.logger.debug("There is already a database connection active.")
			

	def disconnect_db(self):
		"""
			The method disconnects the postgres connection if there is any active. Otherwise ignore it.
		"""
		if self.pgsql_conn:
			self.pgsql_conn.close()
			self.pgsql_conn = None
			
		if self.pgsql_cur:
			self.pgsql_cur = None
	
			
	def set_lock_timeout(self):
		"""
			The method sets the lock timeout using the value stored in the class attribute lock_timeout.
		"""
		self.logger.debug("Changing the lock timeout for the session to %s." % self.lock_timeout)
		self.pgsql_cur.execute("SET LOCK_TIMEOUT =%s;",  (self.lock_timeout, ))
	
	def unset_lock_timeout(self):
		"""
			The method sets the lock timeout using the value stored in the class attribute lock_timeout.
		"""
		self.logger.debug("Disabling the lock timeout for the session." )
		self.pgsql_cur.execute("SET LOCK_TIMEOUT ='0';")
	
	def create_replica_schema(self):
		"""
			The method installs the replica schema sch_ninja if not already  present.
		"""
		self.logger.debug("Trying to connect to the destination database.")
		self.connect_db()
		num_schema = self.check_replica_schema()[0]
		if num_schema == 0:
			self.logger.debug("Creating the replica schema.")
			file_schema = open(self.sql_dir+"create_schema.sql", 'rb')
			sql_schema = file_schema.read()
			file_schema.close()
			self.pgsql_cur.execute(sql_schema)
		
		else:
			self.logger.warning("The replica schema is already present.")
	
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
		self.pgsql_cur.execute(sql_get, (self.i_id_source, ))
		inc_results = self.pgsql_cur.fetchall()
		for table  in inc_results:
			tab_dic = {}
			dic_key = "%s.%s" % (table[0], table[1])
			tab_dic["schema"]  = table[0]
			tab_dic["table"]  = table[1]
			tab_dic["log_seq"]  = int(table[2].split('.')[1])
			tab_dic["log_pos"]  = int(table[3])
			inc_dic[dic_key] = tab_dic
		return inc_dic
	
	def grant_select(self):
		"""
			The method grants the select permissions on all the tables on the replicated schemas to the database roles
			listed in the source's variable grant_select_to.
			In the case a role doesn't exist the method emits an error message and skips the missing user.
		"""
		if self.grant_select_to:
			for schema in  self.schema_loading:
				schema_loading = self.schema_loading[schema]["loading"]
				self.logger.info("Granting select on tables in schema %s to the role(s) %s." % (schema_loading,','.join(self.grant_select_to)))
				for db_role in self.grant_select_to:
					sql_grant_usage = sql.SQL("GRANT USAGE ON SCHEMA {} TO {};").format(sql.Identifier(schema_loading), sql.Identifier(db_role))
					sql_alter_default_privs = sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {};").format(sql.Identifier(schema_loading), sql.Identifier(db_role))
					try:
						self.pgsql_cur.execute(sql_grant_usage)
						self.pgsql_cur.execute(sql_alter_default_privs)
						for table in self.schema_tables[schema]:
							self.logger.info("Granting select on table %s.%s to the role %s." % (schema_loading, table,db_role))
							sql_grant_select = sql.SQL("GRANT SELECT ON TABLE {}.{} TO {};").format(sql.Identifier(schema_loading), sql.Identifier(table), sql.Identifier(db_role))
							try:
								self.pgsql_cur.execute(sql_grant_select)
							except psycopg2.Error as er:
								self.logger.error("SQLCODE: %s SQLERROR: %s" % (er.pgcode, er.pgerror))
					except psycopg2.Error as e:
						if e.pgcode == "42704":
							self.logger.warning("The role %s does not exist" % (db_role, ))
						else:
							self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))

	def replay_replica(self):
		"""
			The method replays the row images in the target database using the function 
			fn_replay_mysql. The function returns a composite type.
			The first element is a boolean flag which
			is true if the batch still require replay. it's false if it doesn't.
			In that case the while loop ends.
			The second element is a, optional list of table names. If any table cause error during the replay
			the problem is captured and the table is removed from the replica. Then the name is returned by
			the function. As the function can find multiple tables with errors during a single replay run, the 
			table names are stored in a list (Actually is a postgres array, see the create_schema.sql file for more details).
			 
			 Each batch which is looped trough can also find multiple tables so we return a list of lists to the replica_engine's
			 calling method.
			
		"""
		tables_error = []
		continue_loop = True
		self.source_config = self.sources[self.source]
		replay_max_rows = self.source_config["replay_max_rows"]
		exit_on_error = True if self.source_config["on_error_replay"]=='exit' else False
		while continue_loop:
			sql_replay = """SELECT * FROM sch_ninja.fn_replay_mysql(%s,%s,%s)""";
			self.pgsql_cur.execute(sql_replay, (replay_max_rows, self.i_id_source, exit_on_error))
			replay_status = self.pgsql_cur.fetchone()
			if replay_status[0]:
				self.logger.debug("Replayed %s rows for source %s" % (replay_max_rows, self.source) )
			continue_loop = replay_status[0]
			function_error = replay_status[1]
			if function_error:
				raise Exception('The replay process crashed')
			if replay_status[2]:
				tables_error.append(replay_status[2])
				
		return tables_error
			
	
	def set_consistent_table(self, table, schema):
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
				AND	v_schema_name = ANY(%s)
			;
		"""
		self.pgsql_cur.execute(sql_set, (self.i_id_source, table, schema))
	
	def get_table_pkey(self, schema, table):
		"""
			The method queries the table sch_ninja.t_replica_tables and gets the primary key 
			associated with the table, if any.
			If there is no primary key the method returns None
			
			:param schema: The table schema
			:param table: The table name
			:return: the primary key associated with the table
			:rtype: list
			
		"""
		sql_pkey = """
			SELECT 
				v_table_pkey
			FROM
				sch_ninja.t_replica_tables
			WHERE
					v_schema_name=%s
				AND	v_table_name=%s
			;
		"""
		self.pgsql_cur.execute(sql_pkey, (schema, table, ))
		table_pkey = self.pgsql_cur.fetchone()
		return table_pkey[0]
		
	def __generate_ddl(self, token,  destination_schema):
		""" 
			The method builds the DDL using the tokenised SQL stored in token.
			The supported commands are 
			RENAME TABLE
			DROP TABLE
			TRUNCATE
			CREATE TABLE
			ALTER TABLE
			DROP PRIMARY KEY
			
			:param token: A dictionary with the tokenised sql statement
			:param destination_schema: The ddl destination schema mapped from the mysql corresponding schema
			:return: query the DDL query in the PostgreSQL dialect
			:rtype: string
			
		"""
		query=""
		if token["command"] =="RENAME TABLE":
			old_name = token["name"]
			new_name = token["new_name"]
			query = """ALTER TABLE "%s"."%s" RENAME TO "%s" """ % (destination_schema, old_name, new_name)	
			table_pkey = self.get_table_pkey(destination_schema, old_name)
			if table_pkey:
				self.store_table(destination_schema, new_name, table_pkey, None)
		elif token["command"] == "DROP TABLE":
			query=""" DROP TABLE IF EXISTS "%s"."%s";""" % (destination_schema, token["name"])	
		elif token["command"] == "TRUNCATE":
			query=""" TRUNCATE TABLE "%s"."%s" CASCADE;""" % (destination_schema, token["name"])	
		elif token["command"] =="CREATE TABLE":
			table_metadata = token["columns"]
			table_name = token["name"]
			index_data = token["indices"]
			table_ddl = self.build_create_table(table_metadata,  table_name,  destination_schema, temporary_schema=False)
			table_enum = ''.join(table_ddl["enum"])
			table_statement = table_ddl["table"] 
			index_ddl = self.build_create_index( destination_schema, table_name, index_data)
			table_pkey = index_ddl[0]
			table_indices = ''.join([val for key ,val in index_ddl[1].items()])
			self.store_table(destination_schema, table_name, table_pkey, None)
			query = "%s %s %s " % (table_enum, table_statement,  table_indices)
		elif token["command"] == "ALTER TABLE":
			query=self.build_alter_table(destination_schema, token)
		elif token["command"] == "DROP PRIMARY KEY":
			self.drop_primary_key(destination_schema, token)
		return query 

	def build_enum_ddl(self, schema, enm_dic):
		"""
			The method builds the enum DDL using the token data. 
			The postgresql system catalog  is queried to determine whether the enum exists and needs to be altered.
			The alter is not written in the replica log table but executed as single statement as PostgreSQL do not allow the alter being part of a multi command
			SQL.
			
			:param schema: the schema where the enumeration is present
			:param enm_dic: a dictionary with the enumeration details
			:return: a dictionary with the pre_alter and post_alter statements (e.g. pre alter create type , post alter drop type)
			:rtype: dictionary
		"""
		enum_name="enum_%s_%s" % (enm_dic['table'], enm_dic['column'])
		
		sql_check_enum = """
			SELECT 
				typ.typcategory,
				typ.typname,
				sch_typ.nspname as typschema,
				CASE 
					WHEN typ.typcategory='E'
					THEN
					(
						SELECT 
							array_agg(enumlabel) 
						FROM 
							pg_enum 
						WHERE 
							enumtypid=typ.oid
					)
				END enum_list
			FROM
				pg_type typ
				INNER JOIN pg_namespace sch_typ
					ON  sch_typ.oid = typ.typnamespace

			WHERE
					sch_typ.nspname=%s
				AND	typ.typname=%s
			;
		"""
		self.pgsql_cur.execute(sql_check_enum, (schema,  enum_name))
		type_data=self.pgsql_cur.fetchone()
		return_dic = {}
		pre_alter = ""
		post_alter = ""
		column_type = enm_dic["type"]
		self.logger.debug(enm_dic)
		if type_data:
			if type_data[0] == 'E' and enm_dic["type"] == 'enum':
				self.logger.debug('There is already the enum %s, altering the type')
				new_enums = [val.strip() for val in enm_dic["enum_list"] if val.strip() not in type_data[3]]
				sql_add = []
				for enumeration in  new_enums:
					sql_add =  """ALTER TYPE "%s"."%s" ADD VALUE '%s';""" % (type_data[2], enum_name, enumeration) 
					self.pgsql_cur.execute(sql_add)
				
			elif type_data[0] != 'E' and enm_dic["type"] == 'enum':
				self.logger.debug('The column will be altered in enum, creating the type')
				pre_alter = """CREATE TYPE "%s"."%s" AS ENUM (%s);""" % (schema,enum_name, enm_dic["enum_elements"])
				
			elif type_data[0] == 'E' and enm_dic["type"] != 'enum':
				self.logger.debug('The column is no longer an enum, dropping the type')
				post_alter = """DROP TYPE "%s"."%s"; """ % (schema,enum_name)
			column_type = """ "%s"."%s" """ % (schema, enum_name)
		elif not type_data and enm_dic["type"] == 'enum':
				self.logger.debug('Creating a new enumeration type %s' % (enum_name))
				pre_alter = """CREATE TYPE "%s"."%s" AS ENUM (%s);""" % (schema,enum_name, enm_dic["enum_elements"])
				column_type = """ "%s"."%s" """ % (schema, enum_name)

		return_dic["column_type"] = column_type
		return_dic["pre_alter"] = pre_alter
		return_dic["post_alter"]  = post_alter
		return return_dic
	

	def build_alter_table(self, schema, token):
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
			
			:param schema: The schema where the affected table is stored on postgres.
			:param token: A dictionary with the tokenised sql statement
			:return: query the DDL query in the PostgreSQL dialect
			:rtype: string
			
		"""
		alter_cmd = []
		ddl_pre_alter = []
		ddl_post_alter = []
		query_cmd=token["command"]
		table_name=token["name"]
		
		for alter_dic in token["alter_cmd"]:
			if alter_dic["command"] == 'DROP':
				alter_cmd.append("%(command)s %(name)s CASCADE" % alter_dic)
			elif alter_dic["command"] == 'ADD':
				
				column_type=self.get_data_type(alter_dic, schema, table_name)
				column_name = alter_dic["name"]
				enum_list = str(alter_dic["dimension"]).replace("'", "").split(",")
				enm_dic = {'table':table_name, 'column':column_name, 'type':column_type, 'enum_list': enum_list, 'enum_elements':alter_dic["dimension"]}
				enm_alter = self.build_enum_ddl(schema, enm_dic)
				ddl_pre_alter.append(enm_alter["pre_alter"])
				ddl_post_alter.append(enm_alter["post_alter"])
				column_type= enm_alter["column_type"]
				if 	column_type in ["character varying", "character", 'numeric', 'bit', 'float']:
						column_type=column_type+"("+str(alter_dic["dimension"])+")"
				if alter_dic["default"]:
					default_value = "DEFAULT %s" % alter_dic["default"]
				else:
					default_value=""
				alter_cmd.append("%s \"%s\" %s NULL %s" % (alter_dic["command"], column_name, column_type, default_value))	
			elif alter_dic["command"] == 'CHANGE':
				sql_rename = ""
				sql_type = ""
				old_column=alter_dic["old"]
				new_column=alter_dic["new"]
				column_name = old_column
				enum_list = str(alter_dic["dimension"]).replace("'", "").split(",")
				
				column_type=self.get_data_type(alter_dic, schema, table_name)
				default_sql = self.generate_default_statements(schema, table_name, old_column, new_column)
				enm_dic = {'table':table_name, 'column':column_name, 'type':column_type, 'enum_list': enum_list, 'enum_elements':alter_dic["dimension"]}
				enm_alter = self.build_enum_ddl(schema, enm_dic)

				ddl_pre_alter.append(enm_alter["pre_alter"])
				ddl_pre_alter.append(default_sql["drop"])
				ddl_post_alter.append(enm_alter["post_alter"])
				ddl_post_alter.append(default_sql["create"])
				column_type= enm_alter["column_type"]
				
				if column_type=="character varying" or column_type=="character" or column_type=='numeric' or column_type=='bit' or column_type=='float':
						column_type=column_type+"("+str(alter_dic["dimension"])+")"
				sql_type = """ALTER TABLE "%s"."%s" ALTER COLUMN "%s" SET DATA TYPE %s  USING "%s"::%s ;;""" % (schema, table_name, old_column, column_type, old_column, column_type)
				if old_column != new_column:
					sql_rename="""ALTER TABLE "%s"."%s" RENAME COLUMN "%s" TO "%s" ;""" % (schema, table_name, old_column, new_column)
					
				query = ' '.join(ddl_pre_alter)
				query += sql_type+sql_rename
				query += ' '.join(ddl_post_alter)
				return query

			elif alter_dic["command"] == 'MODIFY':
				column_type=self.get_data_type(alter_dic, schema, table_name)
				column_name = alter_dic["name"]
				
				enum_list = str(alter_dic["dimension"]).replace("'", "").split(",")
				default_sql = self.generate_default_statements(schema, table_name, column_name)
				enm_dic = {'table':table_name, 'column':column_name, 'type':column_type, 'enum_list': enum_list, 'enum_elements':alter_dic["dimension"]}
				enm_alter = self.build_enum_ddl(schema, enm_dic)

				ddl_pre_alter.append(enm_alter["pre_alter"])
				ddl_pre_alter.append(default_sql["drop"])
				ddl_post_alter.append(enm_alter["post_alter"])
				ddl_post_alter.append(default_sql["create"])
				column_type= enm_alter["column_type"]
				if column_type=="character varying" or column_type=="character" or column_type=='numeric' or column_type=='bit' or column_type=='float':
						column_type=column_type+"("+str(alter_dic["dimension"])+")"
				query = ' '.join(ddl_pre_alter)
				query +=  """ALTER TABLE "%s"."%s" ALTER COLUMN "%s" SET DATA TYPE %s USING "%s"::%s ;""" % (schema, table_name, column_name, column_type, column_name, column_type)
				query += ' '.join(ddl_post_alter)
				return query
		query = ' '.join(ddl_pre_alter)
		query +=  """%s "%s"."%s" %s;""" % (query_cmd , schema,  table_name,', '.join(alter_cmd))
		query += ' '.join(ddl_post_alter)
		return query

	
	def drop_primary_key(self, schema, token):
		"""
			The method drops the primary key for the table.
			As tables without primary key cannot be replicated the method calls unregister_table
			to remove the table from the replica set.
			The drop constraint statement is not built from the token but generated from the information_schema.
			
			:param schema: The table's schema
			:param token: the tokenised query for drop primary key
		"""
		self.logger.info("dropping primary key for table %s.%s" % (schema, token["name"],))
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
				AND table_name=%s
			;
		"""
		self.pgsql_cur.execute(sql_gen, (schema, token["name"]))
		value_check=self.pgsql_cur.fetchone()
		if value_check:
			sql_drop=value_check[0]
			self.pgsql_cur.execute(sql_drop)
			self.unregister_table(schema, token["name"])

	def unregister_table(self, schema,  table):
		"""
			This method is used to remove a table from the replica catalogue.
			The table is just deleted from the table sch_ninja.t_replica_tables.
			
			:param schema: the schema name where the table is stored
			:param table: the table name to remove from t_replica_tables
		"""
		self.logger.info("unregistering table %s.%s from the replica catalog" % (schema, table,))
		sql_delete=""" DELETE FROM sch_ninja.t_replica_tables 
					WHERE
							v_table_name=%s
						AND	v_schema_name=%s
					;
						"""
		self.pgsql_cur.execute(sql_delete, (table, schema))	
	
	def cleanup_source_tables(self):
		"""
			The method cleans up the tables for active source in sch_ninja.t_replica_tables.
			
		"""
		self.logger.info("deleting all the table references from the replica catalog for source %s " % (self.source,))
		sql_delete=""" DELETE FROM sch_ninja.t_replica_tables 
					WHERE
						i_id_source=%s
					;
						"""
		self.pgsql_cur.execute(sql_delete, (self.i_id_source, ))	
	
	def __count_table_schema(self, table, schema):
		"""
			The method checks if the table exists in the given schema.
			
			:param table: the table's name
			:param schema: the postgresql schema where the table should exist
			:return: the count from pg_tables where table name and schema name are the given parameters
			:rtype: integer
		"""
		sql_check = """
			SELECT 
				count(*) 
			FROM 
				pg_tables 
			WHERE 
					schemaname=%s
				AND	tablename=%s;
		"""
		self.pgsql_cur.execute(sql_check, (schema, table ))	
		count_table = self.pgsql_cur.fetchone()
		return count_table[0]
	
	def __count_view_schema(self, view, schema):
		"""
			The method checks if the table exists in the given schema.
			
			:param table: the table's name
			:param schema: the postgresql schema where the table should exist
			:return: the count from pg_tables where table name and schema name are the given parameters
			:rtype: integer
		"""
		sql_check = """
			SELECT 
				count(*) 
			FROM 
				pg_views
			WHERE 
					schemaname=%s
				AND	viewname=%s;
		"""
		self.pgsql_cur.execute(sql_check, (schema, view))	
		count_view = self.pgsql_cur.fetchone()
		return count_view[0]
	
	
	def write_ddl(self, token, query_data,  destination_schema, obfuscated_schema):
		"""
			The method writes the DDL built from the tokenised sql into PostgreSQL.
			
			:param token: the tokenised query
			:param query_data: query's metadata (schema,binlog, etc.)
			:param destination_schema: the postgresql destination schema determined using the schema mappings.
		"""
		drop_create_view = None
		count_table = self.__count_table_schema(token["name"], destination_schema)
		count_view = self.__count_view_schema(token["name"], obfuscated_schema)
		if count_table == 1:
			pg_ddl = self.__generate_ddl(token, destination_schema)
			if count_view == 1:
				ddl_view =  self.__generate_drop_view(token["name"], destination_schema, obfuscated_schema)
				pg_ddl = "%s %s %s" % (ddl_view["drop"], pg_ddl, ddl_view["create"])
		
			self.logger.debug("Translated query: %s " % (pg_ddl,))
			log_table = query_data["log_table"]
			insert_vals = (	
					query_data["batch_id"], 
					token["name"],  
					query_data["schema"], 
					query_data["binlog"], 
					query_data["logpos"], 
					pg_ddl
				)
			sql_insert=sql.SQL("""
				INSERT INTO "sch_ninja".{}
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
				;
			""").format(sql.Identifier(log_table), )
			
			self.pgsql_cur.execute(sql_insert, insert_vals)
		
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
		self.pgsql_cur.execute(sql_batch, (self.i_id_source, self.i_id_source, self.i_id_source, ))
		return self.pgsql_cur.fetchall()
	
	
	def drop_replica_schema(self):
		"""
			The method removes the service schema discarding all the replica references.
			The replicated tables are kept in place though.
		"""
		self.logger.debug("Trying to connect to the destination database.")
		self.connect_db()
		file_schema = open(self.sql_dir+"drop_schema.sql", 'rb')
		sql_schema = file_schema.read()
		file_schema.close()
		self.pgsql_cur.execute(sql_schema)
	
	def check_replica_schema(self):
		"""
			The method checks if the sch_ninja exists
			
			:return: count from information_schema.schemata
			:rtype: integer
		"""
		sql_check="""
			SELECT 
				count(*)
			FROM 
				information_schema.schemata  
			WHERE 
				schema_name='sch_ninja'
		"""
			
		self.pgsql_cur.execute(sql_check)
		num_schema = self.pgsql_cur.fetchone()
		return num_schema

	def get_catalog_version(self):
		"""
			The method returns if the replica schema's version
			
			:return: the version string selected from sch_ninja.v_version
			:rtype: text
		"""
		schema_version = None
		sql_version = """
			SELECT 
				t_version
			FROM 
				sch_ninja.v_version 
			;
		"""
		self.connect_db()
		try:
			self.pgsql_cur.execute(sql_version)
			schema_version = self.pgsql_cur.fetchone()
			self.disconnect_db()
			schema_version = schema_version[0]
		except:
			schema_version = None
		return schema_version
			
	
	def check_schema_mappings(self, exclude_current_source=False):
		"""
			
			The default is false. 
		
			The method checks if there is already a destination schema in the stored schema mappings.
			As each schema should be managed by one mapping only, if the method returns None  then
			the source can be store safely. Otherwise the action. The method doesn't take any decision
			leaving this to the calling methods.
			The method assumes there is a database connection active.
			The method returns a list or none. 
			If the list is returned then contains the count and the destination schema name 
			that are already present in the replica catalogue.
			
			:param exclude_current_source: If set to true the check excludes the current source name from the check.
			:return: the schema already mapped in the replica catalogue. 
			:rtype: list
		"""
		if exclude_current_source:
			exclude_id = self.i_id_source
		else:
			exclude_id = -1
		schema_mappings = json.dumps(self.sources[self.source]["schema_mappings"])
		sql_check = """
			WITH t_check  AS
			(
					SELECT 
						(json_each_text((jsonb_each_text(jsb_schema_mappings)).value::json)).value AS dest_schema
					FROM 
						sch_ninja.t_sources
					WHERE 
						i_id_source <> %s
				UNION ALL
					SELECT DISTINCT
						(json_each_text(value::json)).value
					FROM 
						json_each_text(%s::json) 

			)
			SELECT 
				count(dest_schema),
				dest_schema 
			FROM 
				t_check 
			GROUP BY 
				dest_schema
			HAVING 
				count(dest_schema)>1
			;
		"""
		self.pgsql_cur.execute(sql_check, (exclude_id, schema_mappings, ))
		check_mappings = self.pgsql_cur.fetchone()
		return check_mappings
		
	def check_source(self):
		"""
			The method checks if the source name stored in the class variable self.source is already present.
			As this method is used in both add and drop source it just retuns the count of the sources.
			Any decision about the source is left to the calling method.
			The method assumes there is a database connection active.
			
		"""
		sql_check = """
			SELECT 
				count(*) 
			FROM 
				sch_ninja.t_sources 
			WHERE 
				t_source=%s;
		"""
		self.pgsql_cur.execute(sql_check, (self.source, ))
		num_sources = self.pgsql_cur.fetchone()
		return num_sources[0]
	
	def add_source(self):
		"""
			The method adds a new source to the replication catalog.
			The method calls the function fn_refresh_parts() which generates the log tables used by the replica.
			If the source is already present a warning is issued and no other action is performed.
		"""
		self.logger.debug("Checking if the source %s already exists" % self.source)
		self.connect_db()
		num_sources = self.check_source()
		
		if num_sources == 0:
			check_mappings = self.check_schema_mappings()
			if check_mappings:
				self.logger.error("Could not register the source %s. There is a duplicate destination schema in the schema mappings." % self.source)
			else:
				self.logger.debug("Adding source %s " % self.source)
				schema_mappings = json.dumps(self.sources[self.source]["schema_mappings"])
				log_table_1 = "t_log_replica_%s_1" % self.source
				log_table_2 = "t_log_replica_%s_2" % self.source
				sql_add = """
					INSERT INTO sch_ninja.t_sources 
						( 
							t_source,
							jsb_schema_mappings,
							v_log_table
						) 
					VALUES 
						(
							%s,
							%s,
							ARRAY[%s,%s]
						)
					; 
				"""
				self.pgsql_cur.execute(sql_add, (self.source, schema_mappings, log_table_1, log_table_2))
				
				sql_parts = """SELECT sch_ninja.fn_refresh_parts() ;"""
				self.pgsql_cur.execute(sql_parts)
				self.insert_source_timings()
		else:
			self.logger.warning("The source %s already exists" % self.source)

	def drop_source(self):
		"""
			The method deletes the source from the replication catalogue.
			The log tables are dropped as well, discarding any replica reference for the source.
		"""
		self.logger.debug("Deleting the source %s " % self.source)
		self.connect_db()
		num_sources = self.check_source()
		if num_sources == 1:
			sql_delete = """ DELETE FROM sch_ninja.t_sources 
						WHERE  t_source=%s
						RETURNING v_log_table
						; """
			self.pgsql_cur.execute(sql_delete, (self.source, ))
			source_drop = self.pgsql_cur.fetchone()
			for log_table in source_drop[0]:
				sql_drop = """DROP TABLE sch_ninja."%s"; """ % (log_table)
				try:
					self.pgsql_cur.execute(sql_drop)
				except:
					self.logger.debug("Could not drop the table sch_ninja.%s you may need to remove it manually." % log_table)
		else:
			self.logger.debug("There is no source %s registered in the replica catalogue" % self.source)
			
	def get_schema_list(self):
		"""
			The method gets the list of source schemas for the given source.
			The list is generated using the mapping in sch_ninja.t_sources. 
			Any change in the configuration file is ignored
			The method assumes there is a database connection active.
		"""
		self.logger.debug("Collecting schema list for source %s" % self.source)
		sql_get_schema = """
			SELECT 
				(jsonb_each_text(jsb_schema_mappings)).key
			FROM 
				sch_ninja.t_sources
			WHERE 
				t_source=%s;
			
		"""
		self.pgsql_cur.execute(sql_get_schema, (self.source, ))
		schema_list = [schema[0] for schema in self.pgsql_cur.fetchall()]
		self.logger.debug("Found origin's replication schemas %s" % ', '.join(schema_list))
		return schema_list

	def __build_create_table_pgsql(self, table_metadata,table_name,  schema, temporary_schema=True):
		"""
			The method builds the create table statement with any enumeration or composite type associated to the table
			using the postgresql's metadata.
			The returned value is a dictionary with the optional composite type/enumeration's ddl with the create table without indices or primary keys.
			The method assumes there is a database connection active.
			
			:param table_metadata: the column dictionary extracted from the source's information_schema or builty by the sql_parser class
			:param table_name: the table name 
			:param destination_schema: the schema where the table belongs
			:return: a dictionary with the optional create statements for enumerations and the create table
			:rtype: dictionary
		"""
		table_ddl = {}
		ddl_columns = []
		def_columns = ''
		if temporary_schema:
			destination_schema = self.schema_loading[schema]["loading"]
		else:
			destination_schema = schema
		ddl_head = 'CREATE TABLE "%s"."%s" (' % (destination_schema, table_name)
		ddl_tail = ");"
		ddl_enum=[]
		ddl_composite=[]
		for column in table_metadata:
			column_name = column["column_name"]
			if column["column_default"]:
				default_value = column["column_default"]
			else:
				default_value = ''
			if column["not_null"]:
				col_is_null="NOT NULL"
			else:
				col_is_null="NULL"
			column_type = column["type_format"]
			if column_type == "enum":
				enum_type = '"%s"."enum_%s_%s"' % (destination_schema, table_name[0:20], column["column_name"][0:20])
				sql_drop_enum = 'DROP TYPE IF EXISTS %s CASCADE;' % enum_type
				sql_create_enum = 'CREATE TYPE %s AS ENUM (%s);' % ( enum_type,  column["typ_elements"])
				ddl_enum.append(sql_drop_enum)
				ddl_enum.append(sql_create_enum)
				column_type=enum_type
			if column_type == "composite":
				composite_type = '"%s"."typ_%s_%s"' % (destination_schema, table_name[0:20], column["column_name"][0:20])
				sql_drop_composite = 'DROP TYPE IF EXISTS %s CASCADE;' % composite_type
				sql_create_composite = 'CREATE TYPE %s AS (%s);' % ( composite_type,  column["typ_elements"])
				ddl_composite.append(sql_drop_composite)
				ddl_composite.append(sql_create_composite)
				column_type=composite_type
			if column["col_serial"]:
				default_value = ''
				if column_type == 'bigint':
					column_type = 'bigserial'
				else:
					column_type = 'serial'
				default_value = ''
			ddl_columns.append('"%s" %s %s %s' % (column_name, column_type, default_value, col_is_null))
		def_columns=str(',').join(ddl_columns)
		table_ddl["enum"] = ddl_enum
		table_ddl["composite"] = ddl_composite
		table_ddl["table"] = (ddl_head+def_columns+ddl_tail)
		return table_ddl


	def __build_create_table_mysql(self, table_metadata,table_name,  schema, temporary_schema=True):
		"""
			The method builds the create table statement with any enumeration associated using the mysql's metadata.
			The returned value is a dictionary with the optional enumeration's ddl and the create table without indices or primary keys.
			on the destination schema specified by destination_schema.
			The method assumes there is a database connection active.
			
			:param table_metadata: the column dictionary extracted from the source's information_schema or builty by the sql_parser class
			:param table_name: the table name 
			:param destination_schema: the schema where the table belongs
			:return: a dictionary with the optional create statements for enumerations and the create table
			:rtype: dictionary
		"""
		if temporary_schema:
			destination_schema = self.schema_loading[schema]["loading"]
		else:
			destination_schema = schema
		ddl_head = 'CREATE TABLE "%s"."%s" (' % (destination_schema, table_name)
		ddl_tail = ");"
		ddl_columns = []
		ddl_enum=[]
		table_ddl = {}
		for column in table_metadata:
			if column["is_nullable"]=="NO":
					col_is_null="NOT NULL"
			else:
				col_is_null="NULL"
			column_type = self.get_data_type(column, schema, table_name)
			if column_type == "enum":
				enum_type = '"%s"."enum_%s_%s"' % (destination_schema, table_name[0:20], column["column_name"][0:20])
				sql_drop_enum = 'DROP TYPE IF EXISTS %s CASCADE;' % enum_type
				sql_create_enum = 'CREATE TYPE %s AS ENUM %s;' % ( enum_type,  column["enum_list"])
				ddl_enum.append(sql_drop_enum)
				ddl_enum.append(sql_create_enum)
				column_type=enum_type
			if column_type == "character varying" or column_type == "character":
				column_type="%s (%s)" % (column_type, str(column["character_maximum_length"]))
			if column_type == 'numeric':
				column_type="%s (%s,%s)" % (column_type, str(column["numeric_precision"]), str(column["numeric_scale"]))
			if column["extra"] == "auto_increment":
				column_type = "bigserial"
			ddl_columns.append(  ' "%s" %s %s   ' %  (column["column_name"], column_type, col_is_null ))
		def_columns=str(',').join(ddl_columns)
		table_ddl["enum"] = ddl_enum
		table_ddl["composite"] = []
		table_ddl["table"] = (ddl_head+def_columns+ddl_tail)
		return table_ddl
	
	def build_create_index(self, schema, table, index_data):
		""" 
			The method loops over the list index_data and builds a new list with the statements for the indices.
			
			:param destination_schema: the schema where the table belongs
			:param table_name: the table name 
			:param index_data: the index dictionary used to build the create index statements
			
			:return: a list with the alter and create index for the given table
			:rtype: list
		"""
		idx_ddl = {}
		table_primary = []
		
		for index in index_data:
				table_timestamp = str(int(time.time()))
				indx = index["index_name"]
				self.logger.debug("Generating the DDL for index %s" % (indx))
				index_columns = ['"%s"' % column for column in index["index_columns"]]
				non_unique = index["non_unique"]
				if indx =='PRIMARY':
					pkey_name = "pk_%s_%s_%s " % (table[0:10],table_timestamp,  self.idx_sequence)
					pkey_def = 'ALTER TABLE "%s"."%s" ADD CONSTRAINT "%s" PRIMARY KEY (%s) ;' % (schema, table, pkey_name, ','.join(index_columns))
					idx_ddl[pkey_name] = pkey_def
					table_primary = index["index_columns"]
				else:
					if non_unique == 0:
						unique_key = 'UNIQUE'
						if table_primary == []:
							table_primary = index["index_columns"]
							
					else:
						unique_key = ''
					index_name='idx_%s_%s_%s_%s' % (indx[0:10], table[0:10], table_timestamp, self.idx_sequence)
					idx_def='CREATE %s INDEX "%s" ON "%s"."%s" (%s);' % (unique_key, index_name, schema, table, ','.join(index_columns) )
					idx_ddl[index_name] = idx_def
				self.idx_sequence+=1
		return [table_primary, idx_ddl]

	def get_log_data(self, log_id):
		"""
			The method gets the error log entries, if any, from the replica schema.
			:param log_id: the log id for filtering the row by identifier 
			:return: a dictionary with the errors logged
			:rtype: dictionary
		"""
		self.connect_db()
		if log_id != "*":
			filter_by_logid = self.pgsql_cur.mogrify("WHERE log.i_id_log=%s",  (log_id, ))
		else:
			filter_by_logid = b""
		sql_log = """
			SELECT
				log.i_id_log,
				src.t_source,
				log.i_id_batch,
				log.v_table_name,
				log.v_schema_name,
				log.ts_error,
				log.t_sql,
				log.t_error_message
			FROM 
				sch_ninja.t_error_log log 
				LEFT JOIN sch_ninja.t_sources src
				ON src.i_id_source=log.i_id_source
			%s
		;

		""" % (filter_by_logid.decode())
		
		self.pgsql_cur.execute(sql_log)
		log_error = self.pgsql_cur.fetchall()
		self.disconnect_db()
		return log_error


	def get_status(self):
		"""
			The method gets the status for all sources configured in the target database.
			:return: a list with the status details
			:rtype: list
		"""
		self.connect_db()
		schema_mappings = None
		table_status = None
		if self.source == "*":
			source_filter = ""
			
		else:
			source_filter = (self.pgsql_cur.mogrify(""" WHERE  src.t_source=%s """, (self.source, ))).decode()
			
			sql_mappings = """
				SELECT 
					(mappings).key as origin_schema,
					(((mappings).value)::json)->>'clear' destination_schema,
					(((mappings).value)::json)->>'obfuscate' obfuscated_schema
				FROM

				(
					SELECT 
						jsonb_each_text(jsb_schema_mappings) as mappings
					FROM 
						sch_ninja.t_sources
					WHERE
						t_source=%s

				) sch
				;

			"""
			
			sql_tab_status = """
				WITH  tab_replica AS
				(
					SELECT 
						b_replica_enabled,
						v_schema_name,
						v_table_name
					FROM 
						sch_ninja.t_replica_tables tab
						INNER JOIN sch_ninja.t_sources src
						ON tab.i_id_source=src.i_id_source
						WHERE
							src.t_source=%s
				)
				SELECT
					i_order,
					i_count,
					t_tables
				FROM
				(
					
					SELECT
						0 i_order,
						count(*) i_count,
						array_agg(format('%%I.%%I',v_schema_name,v_table_name)) t_tables
					FROM 
						tab_replica
					WHERE
						NOT b_replica_enabled
				UNION ALL
					SELECT
						1 i_order,
						count(*) i_count,
						array_agg(format('%%I.%%I',v_schema_name,v_table_name)) t_tables
					FROM 
						tab_replica
					WHERE
						b_replica_enabled
				UNION ALL
					SELECT
						2 i_order,
						count(*) i_count,
						array_agg(format('%%I.%%I',v_schema_name,v_table_name)) t_tables
					FROM 
						tab_replica
				) tab_stat
				ORDER BY 
					i_order
			;
			"""
			
			
			self.pgsql_cur.execute(sql_mappings, (self.source, ))
			schema_mappings = self.pgsql_cur.fetchall()
			self.pgsql_cur.execute(sql_tab_status, (self.source, ))
			table_status = self.pgsql_cur.fetchall()
			
			
			
		
		sql_status = """
			SELECT 
				src.i_id_source,
				src.t_source as source_name,
				src.enm_status as  source_status,
				CASE
					WHEN rec.ts_last_received IS NULL
					THEN
						'N/A'::text
					ELSE
						(date_trunc('seconds',now())-ts_last_received)::text
				END AS receive_lag,
				coalesce(rec.ts_last_received::text,''),
				
				CASE
					WHEN rep.ts_last_replayed IS NULL
					THEN
						'N/A'::text
					ELSE
						(rec.ts_last_received-rep.ts_last_replayed)::text
				END AS replay_lag,
				coalesce(rep.ts_last_replayed::text,''),
				CASE
					WHEN src.b_consistent
					THEN
						'Yes'
					ELSE
						'No'
				END as consistent_status
				
				
			FROM 
				sch_ninja.t_sources src
				LEFT JOIN sch_ninja.t_last_received rec
				ON	src.i_id_source = rec.i_id_source
				LEFT JOIN sch_ninja.t_last_replayed rep
				ON	src.i_id_source = rep.i_id_source
			%s
			;
			
		""" % (source_filter, )
		self.pgsql_cur.execute(sql_status)
		configuration_status = self.pgsql_cur.fetchall()
		self.disconnect_db()
		return [configuration_status, schema_mappings, table_status]
		
	def insert_source_timings(self):
		"""
			The method inserts the source timings in the tables t_last_received and t_last_replayed.
			On conflict sets the replay/receive timestamps to null.
			The method assumes there is a database connection active.
		"""
		self.set_source_id()
		sql_replay = """
			INSERT INTO sch_ninja.t_last_replayed
				(
					i_id_source
				)
			VALUES 
				(
					%s
				)
			ON CONFLICT (i_id_source)
			DO UPDATE 
				SET 
					ts_last_replayed=NULL
			;
		"""
		sql_receive = """
			INSERT INTO sch_ninja.t_last_received
				(
					i_id_source
				)
			VALUES 
				(
					%s
				)
			ON CONFLICT (i_id_source)
			DO UPDATE 
				SET 
					ts_last_received=NULL
			;
		"""
		self.pgsql_cur.execute(sql_replay, (self.i_id_source, ))
		self.pgsql_cur.execute(sql_receive, (self.i_id_source, ))

	def __generate_drop_view(self, table, destination_schema, obfuscated_schema):
		"""
			The method generates the drop and create view to wrap around the DDL built from the
			parsed token.
			
			:param table: The table name
			:param destination_schema: The table's schema name
			:param obfuscated_schema: The view's schema name
			:return: the statements for dropping and creating the view which selects from the table
			:rtype: dictionary
		"""
		view_ddl = {}
		query_drop_view = sql.SQL(" DROP VIEW IF EXISTS {}.{} CASCADE;").format(sql.Identifier(obfuscated_schema), sql.Identifier(table))
		query_create_view = sql.SQL(" CREATE OR REPLACE VIEW {}.{} AS SELECT * FROM  {}.{} ;").format(sql.Identifier(obfuscated_schema), sql.Identifier(table),sql.Identifier(destination_schema), sql.Identifier(table))
		view_ddl["drop"] = self.pgsql_cur.mogrify(query_drop_view).decode()	
		view_ddl["create"] = self.pgsql_cur.mogrify(query_create_view).decode()
		return view_ddl

	def  generate_default_statements(self, schema,  table, column, create_column=None):
		"""
			The method gets the default value associated with the table and column removing the cast.
			:param schema: The schema name
			:param table: The table name
			:param column: The column name
			:return: the statements for dropping and creating default value on the affected table
			:rtype: dictionary
		"""
		if not create_column:
			create_column = column
		
		regclass = """ "%s"."%s" """ %(schema, table)
		sql_def_val = """
			SELECT 
				(
					SELECT 
						split_part(substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128),'::',1)
					FROM 
						pg_catalog.pg_attrdef d
					WHERE 
							d.adrelid = a.attrelid 
						AND d.adnum = a.attnum 
						AND a.atthasdef
				) as default_value
				FROM 
					pg_catalog.pg_attribute a
				WHERE 
						a.attrelid = %s::regclass 
					AND a.attname=%s 
					AND NOT a.attisdropped
			;

		"""
		self.pgsql_cur.execute(sql_def_val, (regclass, column ))
		default_value = self.pgsql_cur.fetchone()
		query_drop_default = ""
		query_add_default = ""

		if default_value:
			query_drop_default = sql.SQL(" ALTER TABLE {}.{} ALTER COLUMN {} DROP DEFAULT;").format(sql.Identifier(schema), sql.Identifier(table), sql.Identifier(column))
			query_add_default = sql.SQL(" ALTER TABLE  {}.{} ALTER COLUMN {} SET DEFAULT %s;").format(sql.Identifier(schema), sql.Identifier(table), sql.Identifier(column))
			
			query_drop_default = self.pgsql_cur.mogrify(query_drop_default)
			query_add_default = self.pgsql_cur.mogrify(query_add_default, (default_value[0], ))
		
		return {'drop':query_drop_default.decode(), 'create':query_add_default.decode()}



	def get_data_type(self, column, schema,  table):
		""" 
			The method determines whether the specified type has to be overridden or not.
			
			:param column: the column dictionary extracted from the information_schema or built in the sql_parser class
			:param schema: the schema name 
			:param table: the table name 
			:return: the postgresql converted column type
			:rtype: string
		"""
		if self.type_override:
			try:
				
				table_full = "%s.%s" % (schema, table)
				type_override = self.type_override[column["column_type"]]
				override_to = type_override["override_to"]
				override_tables = type_override["override_tables"]
				if override_tables[0] == '*' or table_full in override_tables:
					column_type = override_to
				else:
					column_type = self.type_dictionary[column["data_type"]]
			except KeyError:
				column_type = self.type_dictionary[column["data_type"]]
		else:
			column_type = self.type_dictionary[column["data_type"]]
		return column_type
	
	def set_application_name(self, action=""):
		"""
			The method sets the application name in the replica using the variable self.pg_conn.global_conf.source_name,
			Making simpler to find the replication processes. If the source name is not set then a generic PGCHAMELEON name is used.
		"""
		if self.source:
			app_name = "[pg_ninja] - source: %s, action: %s" % (self.source, action)
		else:
			app_name = "[pg_ninja] -  action: %s" % (action) 
		sql_app_name="""SET application_name=%s; """
		self.pgsql_cur.execute(sql_app_name, (app_name , ))
		
	def write_batch(self, group_insert):
		"""
			Main method for adding the batch data in the log tables. 
			The row data from group_insert are mogrified in CSV format and stored in
			the string like object csv_file.
			
			psycopg2's copy expert is used to store the event data in PostgreSQL.
			
			Should any error occur the procedure fallsback to insert_batch.
			
			:param group_insert: the event data built in mysql_engine
		"""
		csv_file=io.StringIO()
		self.set_application_name("writing batch")
		insert_list=[]
		for row_data in group_insert:
			global_data=row_data["global_data"]
			event_after=row_data["event_after"]
			event_before=row_data["event_before"]
			log_table=global_data["log_table"]
			insert_list.append(self.pgsql_cur.mogrify("%s,%s,%s,%s,%s,%s,%s,%s,%s" ,  (
						global_data["batch_id"], 
						global_data["table"],  
						global_data["schema"], 
						global_data["action"], 
						global_data["binlog"], 
						global_data["logpos"], 
						json.dumps(event_after, cls=pg_encoder), 
						json.dumps(event_before, cls=pg_encoder), 
						global_data["event_time"], 
						
					)
				)
			)
											
		csv_data=b"\n".join(insert_list ).decode()
		csv_file.write(csv_data)
		csv_file.seek(0)
		try:
			sql_copy=sql.SQL("""
				COPY "sch_ninja".{}
					(
						i_id_batch, 
						v_table_name, 
						v_schema_name, 
						enm_binlog_event, 
						t_binlog_name, 
						i_binlog_position, 
						jsb_event_after,
						jsb_event_before,
						i_my_event_time
					) 
				FROM 
					STDIN 
					WITH NULL 'NULL' 
					CSV QUOTE '''' 
					DELIMITER ',' 
					ESCAPE '''' 
				;
			""").format(sql.Identifier(log_table))
			self.pgsql_cur.copy_expert(sql_copy,csv_file)
		except psycopg2.Error as e:
			self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
			self.logger.error("fallback to inserts")
			self.insert_batch(group_insert)
		self.set_application_name("idle")
	
	def insert_batch(self,group_insert):
		"""
			Fallback method for the batch insert. Each row event is processed
			individually and any problematic row is discarded into the table t_discarded_rows.
			The row is encoded in base64 in order to prevent any encoding or type issue.
			
			:param group_insert: the event data built in mysql_engine
		"""
		
		self.logger.debug("starting insert loop")
		for row_data in group_insert:
			global_data = row_data["global_data"]
			event_after= row_data["event_after"]
			event_before= row_data["event_before"]
			log_table = global_data["log_table"]
			event_time = global_data["event_time"]
			sql_insert=sql.SQL("""
				INSERT INTO sch_ninja.{}
					(
						i_id_batch, 
						v_table_name, 
						v_schema_name, 
						enm_binlog_event, 
						t_binlog_name, 
						i_binlog_position, 
						jsb_event_after,
						jsb_event_before,
						i_my_event_time
					)
					VALUES 
						(
							%s,
							%s,
							%s,
							%s,
							%s,
							%s,
							%s,
							%s,
							%s
						)
				;						
			""").format(sql.Identifier(log_table))
			try:
				self.pgsql_cur.execute(sql_insert,(
						global_data["batch_id"], 
						global_data["table"],  
						global_data["schema"], 
						global_data["action"], 
						global_data["binlog"], 
						global_data["logpos"], 
						json.dumps(event_after, cls=pg_encoder), 
						json.dumps(event_before, cls=pg_encoder), 
						event_time
					)
				)
			except psycopg2.Error as e:
				if e.pgcode == "22P05":
					self.logger.warning("%s - %s. Trying to cleanup the row" % (e.pgcode, e.pgerror))
					event_after = {key: str(value).replace("\x00", "") for key, value in event_after.items()}
					event_before = {key: str(value).replace("\x00", "") for key, value in event_before.items()}
					try:
						self.pgsql_cur.execute(sql_insert,(
								global_data["batch_id"], 
								global_data["table"],  
								global_data["schema"], 
								global_data["action"], 
								global_data["binlog"], 
								global_data["logpos"], 
								json.dumps(event_after, cls=pg_encoder), 
								json.dumps(event_before, cls=pg_encoder), 
								event_time
							)
						)
					except:
						self.logger.error("Cleanup unsuccessful. Saving the discarded row")
						self.save_discarded_row(row_data)
				else:
					self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
					self.logger.error("Error when storing event data. Saving the discarded row")
					self.save_discarded_row(row_data)
			except:
				self.logger.error("Error when storing event data. Saving the discarded row")
				self.save_discarded_row(row_data)

	def save_discarded_row(self,row_data):
		"""
			The method saves the discarded row in the table t_discarded_row along with the id_batch.
			The row is encoded in base64 as the t_row_data is a text field.
			
			:param row_data: the row data dictionary
			
		"""
		global_data = row_data["global_data"]
		schema = global_data["schema"]
		table  = global_data["table"]
		batch_id = global_data["batch_id"]
		str_data = '%s' %(row_data, )
		hex_row = binascii.hexlify(str_data.encode())
		sql_save="""
			INSERT INTO sch_ninja.t_discarded_rows
				(
					i_id_batch, 
					v_schema_name,
					v_table_name,
					t_row_data
				)
			VALUES 
				(
					%s,
					%s,
					%s,
					%s
				);
		"""
		self.pgsql_cur.execute(sql_save,(batch_id, schema, table,hex_row))
		self.pgsql_cur.execute(sql_save,(batch_id, schema, table,b64_row))
	
	
	def create_table(self,  table_metadata,table_name,  schema, metadata_type):
		"""
			Executes the create table returned by build_create_table on the destination_schema.
			
			:param table_metadata: the column dictionary extracted from the source's information_schema or builty by the sql_parser class
			:param table_name: the table name 
			:param destination_schema: the schema where the table belongs
			:param metadata_type: the metadata type, currently supported mysql and pgsql
		"""
		if metadata_type == 'mysql':
			table_ddl = self.__build_create_table_mysql( table_metadata,table_name,  schema)
		elif metadata_type == 'pgsql':
			table_ddl = self.__build_create_table_pgsql( table_metadata,table_name,  schema)
		enum_ddl = table_ddl["enum"] 
		composite_ddl = table_ddl["composite"] 
		table_ddl = table_ddl["table"] 
		
		for enum_statement in enum_ddl:
			self.pgsql_cur.execute(enum_statement)
		
		for composite_statement in composite_ddl:
			self.pgsql_cur.execute(composite_statement)

		self.pgsql_cur.execute(table_ddl)
	
	
	def alter_obfuscated_fields(self, table, schema_clear, schema_obfuscated):
		"""
			The method alter the table in the obfuscated schema using the informations from the schema in clear.
			All the character varying fields are converted in test. All the not null constraints are dropped except for the
			fields used in primary keys.
			
			:param table: the table name 
			:param schema_clear: the schema in clear where the table is created
			:param schema_obfuscated: the schema with the obfuscated table that needs to be altered
		"""
		
		sql_gen_alter = """
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
						AND	keycol.table_name=fil.table_name
				)

				SELECT 
					format('ALTER TABLE %%I.%%I ALTER COLUMN %%I TYPE text ;',
					%s,
					col.table_name,
					col.column_name
					) AS alter_table
				FROM
					information_schema.columns col
					INNER JOIN t_filter fil
					ON
							col.table_schema=fil.table_schema
						AND	col.table_name=fil.table_name
				WHERE 
						column_name NOT IN (
							SELECT 
							column_name 
							FROM
							t_key
						)
					AND	data_type = 'character varying'
			UNION ALL

				SELECT 
					format('ALTER TABLE %%I.%%I ALTER COLUMN %%I DROP NOT NULL;',
					%s,
					col.table_name,
					col.column_name
					) AS alter_table
				FROM
					information_schema.columns col
					INNER JOIN t_filter fil
					ON 
							col.table_schema=fil.table_schema
						AND	col.table_name=fil.table_name
				WHERE 
					column_name NOT IN (
							SELECT 
							column_name 
							FROM
							t_key
						)
					AND	is_nullable = 'NO'
			;
		"""
		self.pgsql_cur.execute(sql_gen_alter, (schema_clear, table, schema_obfuscated,schema_obfuscated ))
		alter_stats = self.pgsql_cur.fetchall()
		for alter in alter_stats:
			self.pgsql_cur.execute(alter[0])

	def create_clear_view(self, schema, table):
		"""
			The method create the views in the obfuscated_loading schema for the tables with data in clear.
		"""
		
		schema_loading = self.schema_loading[schema]["loading"]
		schema_obfuscated_loading = self.schema_loading[schema]["loading_obfuscated"]
		sql_get_create="""
			SELECT 
					format('CREATE OR REPLACE VIEW %%I.%%I AS SELECT * FROM %%I.%%I ;',
						%s,
						table_name,
						table_schema,
						table_name
					) as create_view,
					table_name,
					table_schema
			FROM
				information_schema.TABLES 
			WHERE 
			table_schema=%s
			AND table_name=%s
			;
		"""
		self.pgsql_cur.execute(sql_get_create, (schema_obfuscated_loading,  schema_loading, table))
		view_stat = self.pgsql_cur.fetchone()
		self.logger.info("creating view %s.%s" % (schema_obfuscated_loading, table))
		self.pgsql_cur.execute(view_stat[0])

	def store_obfuscated_table(self, table, schema):
		"""
			The method trie to remove the obfuscated table from the replication catalogue and then copies the data from the existing
			table in clear changing the schema.
			The binlog positions are preserved.
			
			:param table_name: the table name 
			:param schema: the original mysql schema where the table belongs. this value is used as key to determine the two loading schemas
		"""
		schema_clear = self.schema_loading[schema]["destination"]
		schema_obfuscated= self.schema_loading[schema]["obfuscated"]
		
		sql_delete = """
			DELETE FROM sch_ninja.t_replica_tables
			WHERE
					v_table_name=%s
				AND	v_schema_name=%s
				AND i_id_source=%s
		"""
		self.pgsql_cur.execute(sql_delete, (table, schema_obfuscated, self.i_id_source))
		
		sql_insert = """
			INSERT INTO sch_ninja.t_replica_tables
			(
				i_id_source,
				v_schema_name,
				v_table_name,
				v_table_pkey,
				t_binlog_name,
				i_binlog_position,
				b_replica_enabled
			)
			SELECT
				i_id_source,
				%s::text AS v_schema_name,
				v_table_name,
				v_table_pkey,
				t_binlog_name,
				i_binlog_position,
				b_replica_enabled
			FROM
				sch_ninja.t_replica_tables
			WHERE
					v_schema_name=%s
				AND	v_table_name=%s
				AND	i_id_source=%s
			;
		"""
		self.pgsql_cur.execute(sql_insert, (schema_obfuscated, schema_clear, table, self.i_id_source))
		
	def create_obfuscated_indices(self, table, schema):
		"""
			The method builds the indices on the obfuscated table using the table in clear as a template.
			
			:param table_name: the table name 
			:param schema: the original mysql schema where the table belongs. this value is used as key to determine the two loading schemas
		"""
		schema_loading = self.schema_loading[schema]["loading"]
		schema_obfuscated_loading = self.schema_loading[schema]["loading_obfuscated"]
		
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
		self.pgsql_cur.execute(sql_get_idx, (table, schema_loading, schema_obfuscated_loading ) )
		build_idx = self.pgsql_cur.fetchall()
		build_idx = [ idx[0] for idx in build_idx ]
		for idx in build_idx:
			try:
				self.logger.info("Executing: %s" % (idx))
				self.pgsql_cur.execute(idx)
			except:
				self.logger.error("Couldn't add the index to the table %s. \nIndex definition: %s" % (table, idx))


	def copy_obfuscated_table(self, table,  schema, table_obfuscation):
		"""
			The method copies the obfuscated data from the schema loading in clear to the schema loading with obfuscated data.
			
			:param table_name: the table name 
			:param schema: the original mysql schema where the table belongs. this value is used as key to determine the two loading schemas
			:table_obfuscation: dictionary with the table's obfuscation mapping
		"""
		schema_loading = self.schema_loading[schema]["loading"]
		schema_obfuscated_loading = self.schema_loading[schema]["loading_obfuscated"]
		
		sql_crypto = "SELECT count(*) FROM pg_catalog.pg_extension where extname='pgcrypto';"
		self.pgsql_cur.execute(sql_crypto)
		pg_crypto = self.pgsql_cur.fetchone()
		if pg_crypto[0] == 0:
			self.logger.warning("Extension pgcrypto missing on database. aborting the obfuscation")
			return
		self.logger.info("Obfuscating the table %s.%s" % (schema, table))
		col_list=[]
		sql_cols = """ 
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
		self.pgsql_cur.execute(sql_cols, (schema_obfuscated_loading,table ))
		columns = self.pgsql_cur.fetchall()
		for column in columns:
			try:
				obfdata = table_obfuscation[column[0]]
				if obfdata["mode"] == "normal":
					col_list.append("(substr(\"%s\"::text, %s , %s)||encode(public.digest(\"%s\",'sha256'),'hex'))%s" %(column[0], obfdata["nonhash_start"], obfdata["nonhash_length"], column[0],  column[1]))
				elif obfdata["mode"] == "date":
					col_list.append("to_char(\"%s\"::date,'YYYY-01-01')::date" % (column[0]))
				elif obfdata["mode"] == "numeric":
					col_list.append("0%s" % (column[1]))
				elif obfdata["mode"] == "setnull":
					col_list.append("NULL%s" % (column[1]))
			except:
				col_list.append('"%s"'%(column[0], ))
		sql_insert = "INSERT INTO  {}.{} SELECT %s FROM {}.{};" % ','.join(col_list) 
		sql_copy = sql.SQL(sql_insert).format(sql.Identifier(schema_obfuscated_loading),sql.Identifier(table),sql.Identifier(schema_loading), sql.Identifier(table),)
		self.pgsql_cur.execute(sql_copy)
	
		
	def create_obfuscated_table(self,  table_name,  schema):
		"""
			Builds a new table in the obfuscated loading schema using the clear loading schema's definition
			
			:param table_name: the table name 
			:param schema: the original mysql schema where the table belongs. this value is used as key to determine the two loading schemas
		"""
		schema_loading = self.schema_loading[schema]["loading"]
		schema_obfuscated_loading = self.schema_loading[schema]["loading_obfuscated"]
		sql_create_table = sql.SQL("""
			CREATE TABLE {}.{}
				(LIKE {}.{})
		;
		""").format(sql.Identifier(schema_obfuscated_loading), sql.Identifier(table_name), sql.Identifier(schema_loading), sql.Identifier(table_name))
		self.pgsql_cur.execute(sql_create_table)
		self.alter_obfuscated_fields(table_name, schema_loading, schema_obfuscated_loading)
		
	
	def update_schema_mappings(self):
		"""
			The method updates the schema mappings for the given source.
			Before executing the updates the method checks for the need to run an update and for any
			mapping already present in the replica catalogue. 
			If everything is fine the database connection is set autocommit=false.
			The method updates the schemas  in the table t_replica_tables and then updates the mappings in the 
			table t_sources. After the final update the commit is issued to make the updates permanent.
			
			:todo: The method should run only at replica stopped for the given source. The method should also  replay all the logged rows for the given source before updating the schema mappings to avoid  to get an inconsistent replica.
		"""
		self.connect_db()
		self.set_source_id()
		self.replay_replica()
		new_schema_mappings = self.sources[self.source]["schema_mappings"]
		old_schema_mappings = self.get_schema_mappings()
		
		
		if new_schema_mappings != old_schema_mappings:
			duplicate_mappings = self.check_schema_mappings(True)
			if not duplicate_mappings:
				self.logger.debug("Updating schema mappings for source %s" % self.source)
				self.set_autocommit_db(False)
				for schema in old_schema_mappings:
					old_mapping = old_schema_mappings[schema]
					try:
						new_mapping = new_schema_mappings[schema]
					except KeyError:
						new_mapping = None
					if not new_mapping:
						self.logger.debug("The mapping for schema %s has ben removed. Deleting the reference from the replica catalogue." % (schema))
						sql_delete = """
							DELETE FROM sch_ninja.t_replica_tables 
							WHERE 	
									i_id_source=%s
								AND	v_schema_name=%s
							;
						"""
						self.pgsql_cur.execute(sql_delete, (self.i_id_source,old_mapping ))
					elif old_mapping != new_mapping:
						self.logger.debug("Updating mapping for schema %s. Old: %s. New: %s" % (schema, old_mapping, new_mapping))
						sql_tables = """
							UPDATE sch_ninja.t_replica_tables 
								SET v_schema_name=%s
							WHERE 	
									i_id_source=%s
								AND	v_schema_name=%s
							;
						"""
						self.pgsql_cur.execute(sql_tables, (new_mapping, self.i_id_source,old_mapping ))
						sql_alter_schema = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(old_mapping), sql.Identifier(new_mapping))
						self.pgsql_cur.execute(sql_alter_schema)
				sql_source="""
					UPDATE sch_ninja.t_sources
						SET 
							jsb_schema_mappings=%s
					WHERE
						i_id_source=%s
					;
							
				"""
				self.pgsql_cur.execute(sql_source, (json.dumps(new_schema_mappings), self.i_id_source))
				self.pgsql_conn.commit()
					
				self.set_autocommit_db(True)
			else:
				self.logger.error("Could update the schema mappings for source %s. There is a duplicate destination schema in other sources. The offending schema is %s." % (self.source, duplicate_mappings[1]))
		else:
			self.logger.debug("The configuration file and catalogue mappings for source %s are the same. Not updating." % self.source)
		#print (self.i_id_source)
		
		self.disconnect_db()
	
	def get_schema_mappings(self):
		"""
			The method gets the schema mappings for the given source.
			The list is the one stored in the table sch_ninja.t_sources. 
			Any change in the configuration file is ignored
			The method assumes there is a database connection active.
			:return: the schema mappings extracted from the replica catalogue
			:rtype: dictionary
	
		"""
		self.logger.debug("Collecting schema mappings for source %s" % self.source)
		sql_get_schema = """
			SELECT 
				jsb_schema_mappings
			FROM 
				sch_ninja.t_sources
			WHERE 
				t_source=%s;
			
		"""
		self.pgsql_cur.execute(sql_get_schema, (self.source, ))
		schema_mappings = self.pgsql_cur.fetchone()
		return schema_mappings[0]
	
	def set_source_status(self, source_status):
		"""
			The method updates the source status for the source_name and sets the class attribute i_id_source.
			The method assumes there is a database connection active.
			
			:param source_status: The source status to be set.
			
		"""
		sql_source = """
			UPDATE sch_ninja.t_sources
			SET
				enm_status=%s
			WHERE
				t_source=%s
			RETURNING i_id_source
				;
			"""
		self.pgsql_cur.execute(sql_source, (source_status, self.source, ))
		source_data = self.pgsql_cur.fetchone()
		

		try:
			self.i_id_source = source_data[0]
		except:
			print("Source %s is not registered." % self.source)
			sys.exit()
	
	def set_source_id(self):
		"""
			The method sets the class attribute i_id_source for the self.source.
			The method assumes there is a database connection active.
		"""
		sql_source = """
			SELECT i_id_source FROM 
				sch_ninja.t_sources
			WHERE
				t_source=%s
			;
			"""
		self.pgsql_cur.execute(sql_source, ( self.source, ))
		source_data = self.pgsql_cur.fetchone()
		try:
			self.i_id_source = source_data[0]
		except:
			print("Source %s is not registered." % self.source)
			sys.exit()
	
	
	def clean_batch_data(self):
		"""
			This method removes all the batch data for the source id stored in the class varible self.i_id_source.
			
			The method assumes there is a database connection active.
		"""
		sql_cleanup = """
			DELETE FROM sch_ninja.t_replica_batch WHERE i_id_source=%s;
		"""
		self.pgsql_cur.execute(sql_cleanup, (self.i_id_source, ))


	def check_source_consistent(self):
		"""
			This method checks if the database is consistent using the source's high watermark and the 
			source's flab b_consistent.
			If the batch data is larger than the source's high watermark then the source is marked consistent and
			all the log data stored witth the source's tables are set to null in order to ensure all the tables are replicated.
		"""
		
		sql_check_consistent = """
			WITH hwm AS
				(
					SELECT 
						split_part(t_binlog_name,'.',2)::integer as i_binlog_sequence,
						i_binlog_position 
					FROM 
						sch_ninja.t_sources
					WHERE
							i_id_source=%s
						AND	not b_consistent

				)
			SELECT 
				CASE
					WHEN	bat.binlog_data[1]>hwm.i_binlog_sequence
					THEN 
						True
					WHEN		bat.binlog_data[1]=hwm.i_binlog_sequence
						AND	bat.binlog_data[2]>=hwm.i_binlog_position
					THEN 
						True
					ELSE
						False
				END AS b_consistent 
			FROM
				(
					SELECT 
						max(
							array[
								split_part(t_binlog_name,'.',2)::integer, 
								i_binlog_position
							]
						) as binlog_data
					FROM 
						sch_ninja.t_replica_batch
					WHERE
							i_id_source=%s
						AND	b_started
						AND	b_processed

				) bat,
				hwm
			;

		"""
		self.pgsql_cur.execute(sql_check_consistent, (self.i_id_source, self.i_id_source, ))
		self.logger.debug("Checking consistent status for source: %s" %(self.source, ) )
		source_consistent = self.pgsql_cur.fetchone()
		if source_consistent:
			if source_consistent[0]:
				self.logger.info("The source: %s reached the consistent status" %(self.source, ) )
				sql_set_source_consistent = """
					UPDATE sch_ninja.t_sources
						SET
							b_consistent=True,
							t_binlog_name=NULL,
							i_binlog_position=NULL
					WHERE
						i_id_source=%s
				;
				"""
				sql_set_tables_consistent = """
					UPDATE sch_ninja.t_replica_tables
						SET
							t_binlog_name=NULL,
							i_binlog_position=NULL
					WHERE
						i_id_source=%s
				;
				"""
				self.pgsql_cur.execute(sql_set_source_consistent, (self.i_id_source,  ))
				self.pgsql_cur.execute(sql_set_tables_consistent, (self.i_id_source,  ))
			else:
				self.logger.debug("The source: %s is not consistent " %(self.source, ) )
		else:
			self.logger.debug("The source: %s is consistent" %(self.source, ) )
	
	def set_source_highwatermark(self, master_status, consistent):
		"""
			This method saves the master data within the source.
			The values are used to determine whether the database has reached the consistent point.
			
			:param master_status: the master data with the binlogfile and the log position
		"""
		master_data = master_status[0]
		binlog_name = master_data["File"]
		binlog_position = master_data["Position"]
		sql_set  = """
			UPDATE sch_ninja.t_sources
				SET 
					b_consistent=%s,
					t_binlog_name=%s,
					i_binlog_position=%s
			WHERE
				i_id_source=%s
			;
					
		"""
		self.pgsql_cur.execute(sql_set, (consistent, binlog_name, binlog_position, self.i_id_source, ))
		self.logger.info("Set high watermark for source: %s" %(self.source, ) )
		

	def save_master_status(self, master_status):
		"""
			This method saves the master data determining which log table should be used in the next batch.
			The method assumes there is a database connection active.
			
			:param master_status: the master data with the binlogfile and the log position
			:return: the batch id or none if no batch has been created
			:rtype: integer
		"""
		next_batch_id = None
		master_data = master_status[0]
		binlog_name = master_data["File"]
		binlog_position = master_data["Position"]
		try:
			event_time = master_data["Time"]
		except:
			event_time = None
		
		sql_master = """
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
		
		sql_log_table="""
			UPDATE sch_ninja.t_sources 
			SET 
				v_log_table=ARRAY[v_log_table[2],v_log_table[1]]
				
			WHERE 
				i_id_source=%s
			RETURNING 
				v_log_table[1]
			; 
		"""

		sql_last_update = """
			UPDATE 
				sch_ninja.t_last_received  
			SET 
				ts_last_received=to_timestamp(%s)
			WHERE 
				i_id_source=%s
			RETURNING ts_last_received
		;
		"""
		
		try:
			self.pgsql_cur.execute(sql_master, (self.i_id_source, binlog_name, binlog_position))
			results =self.pgsql_cur.fetchone()
			next_batch_id=results[0]
			self.pgsql_cur.execute(sql_log_table, (self.i_id_source, ))
			results = self.pgsql_cur.fetchone()
			log_table_name = results[0]
			self.pgsql_cur.execute(sql_last_update, (event_time, self.i_id_source, ))
			results = self.pgsql_cur.fetchone()
			db_event_time = results[0]
			self.logger.info("Saved master data for source: %s" %(self.source, ) )
			self.logger.debug("Binlog file: %s" % (binlog_name, ))
			self.logger.debug("Binlog position:%s" % (binlog_position, ))
			self.logger.debug("Last event: %s" % (db_event_time, ))
			self.logger.debug("Next log table name: %s" % ( log_table_name, ))
			
		except psycopg2.Error as e:
					self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
					self.logger.error(self.pgsql_cur.mogrify(sql_master, (self.i_id_source, binlog_name, binlog_position)))
		
		return next_batch_id

	
	def store_table(self, schema, table, table_pkey, master_status):
		"""
			The method saves the table name along with the primary key definition in the table t_replica_tables.
			This is required in order to let the replay procedure which primary key to use replaying the update and delete.
			If the table is without primary key is not stored. 
			A table without primary key is copied and the indices are create like any other table. 
			However the replica doesn't work for the tables without primary key.
			
			If the class variable master status is set then the master's coordinates are saved along with the table.
			This happens in general when a table is added to the replica or the data is refreshed with sync_tables.
			
			:param schema: the schema name to store in the table  t_replica_tables
			:param table: the table name to store in the table  t_replica_tables
			:param table_pkey: a list with the primary key's columns. empty if there's no pkey
			:param master_status: the master status data .
		"""
		if master_status:
			master_data = master_status[0]
			binlog_file = master_data["File"]
			binlog_pos = master_data["Position"]
		else:
			binlog_file = None
			binlog_pos = None
			
		
		if len(table_pkey) > 0:
			sql_insert = """ 
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
						%s,
						%s,
						%s
					)
				ON CONFLICT (i_id_source,v_table_name,v_schema_name)
					DO UPDATE 
						SET 
							v_table_pkey=EXCLUDED.v_table_pkey,
							t_binlog_name = EXCLUDED.t_binlog_name,
							i_binlog_position = EXCLUDED.i_binlog_position,
							b_replica_enabled = True
				;
							"""
			self.pgsql_cur.execute(sql_insert, (
				self.i_id_source, 
				table, 
				schema, 
				table_pkey, 
				binlog_file, 
				binlog_pos
				)
			)
		else:
			self.logger.warning("Missing primary key. The table %s.%s will not be replicated." % (schema, table,))
			self.unregister_table(schema,  table)

	
	def copy_data(self, csv_file, schema, table, column_list):
		"""
			The method copy the data into postgresql using psycopg2's copy_expert.
			The csv_file is a file like object which can be either a  csv file or a string io object, accordingly with the 
			configuration parameter copy_mode.
			The method assumes there is a database connection active.
			
			:param csv_file: file like object with the table's data stored in CSV format
			:param schema: the schema used in the COPY FROM command
			:param table: the table name used in the COPY FROM command
			:param column_list: A string with the list of columns to use in the COPY FROM command already quoted and comma separated
		"""
		sql_copy='COPY "%s"."%s" (%s) FROM STDIN WITH NULL \'NULL\' CSV QUOTE \'"\' DELIMITER \',\' ESCAPE \'"\' ; ' % (schema, table, column_list)		
		self.pgsql_cur.copy_expert(sql_copy,csv_file)
		
	def insert_data(self, schema, table, insert_data , column_list):
		"""
			The method is a fallback procedure for when the copy method fails.
			The procedure performs a row by row insert, very slow but capable to skip the rows with problematic data (e.g. encoding issues).
			
			:param schema: the schema name where table belongs
			:param table: the table name where the data should be inserted
			:param insert_data: a list of records extracted from the database using the unbuffered cursor
			:param column_list: the list of column names quoted  for the inserts
		"""
		sample_row = insert_data[0]
		column_marker=','.join(['%s' for column in sample_row])
		
		sql_head='INSERT INTO "%s"."%s"(%s) VALUES (%s);' % (schema, table, column_list, column_marker)
		for data_row in insert_data:
			try:
				self.pgsql_cur.execute(sql_head,data_row)	
			except psycopg2.Error as e:
					self.logger.error("SQLCODE: %s SQLERROR: %s" % (e.pgcode, e.pgerror))
					self.logger.error(self.pgsql_cur.mogrify(sql_head,data_row))
			except ValueError:
				self.logger.warning("character mismatch when inserting the data, trying to cleanup the row data")
				data_row = [str(item).replace("\x00", "") for item in data_row]
				try:
					self.pgsql_cur.execute(sql_head,data_row)	
				except:
					self.logger.error("error when inserting the row, skipping the row")
					
			except:
				self.logger.error("unexpected error when processing the row")
				self.logger.error(" - > Table: %s.%s" % (schema, table))
				
	
	def create_indices(self, schema, table, index_data):
		"""
			The method loops odver the list index_data and creates the indices on the table 
			specified with schema and table parameters.
			The method assumes there is a database connection active.
			
			:param schema: the schema name where table belongs
			:param table: the table name where the data should be inserted
			:param index_data: a list of dictionaries with the index metadata for the given table.
			:return: a list with the eventual column(s) used as primary key
			:rtype: list
		"""
		idx_ddl = {}
		table_primary = []
		for index in index_data:
				table_timestamp = str(int(time.time()))
				indx = index["index_name"]
				self.logger.debug("Building DDL for index %s" % (indx))
				idx_col = [column.strip() for column in index["index_columns"].split(',')]
				index_columns = ['"%s"' % column.strip() for column in idx_col]
				non_unique = index["non_unique"]
				if indx =='PRIMARY':
					pkey_name = "pk_%s_%s_%s " % (table[0:10],table_timestamp,  self.idx_sequence)
					pkey_def = 'ALTER TABLE "%s"."%s" ADD CONSTRAINT "%s" PRIMARY KEY (%s) ;' % (schema, table, pkey_name, ','.join(index_columns))
					idx_ddl[pkey_name] = pkey_def
					table_primary = idx_col
				else:
					if non_unique == 0:
						unique_key = 'UNIQUE'
						if table_primary == []:
							table_primary = idx_col
					else:
						unique_key = ''
					index_name='idx_%s_%s_%s_%s' % (indx[0:10], table[0:10], table_timestamp, self.idx_sequence)
					idx_def='CREATE %s INDEX "%s" ON "%s"."%s" (%s);' % (unique_key, index_name, schema, table, ','.join(index_columns) )
					idx_ddl[index_name] = idx_def
				self.idx_sequence+=1
		for index in idx_ddl:
			self.logger.info("Building index %s on %s.%s" % (index, schema, table))
			self.pgsql_cur.execute(idx_ddl[index])	
			
		return table_primary	
		
	def swap_schemas(self):
		"""
			The method  loops over the schema_loading class dictionary and 
			swaps the loading with the destination schemas performing a double rename.
			The method assumes there is a database connection active.
		"""
		for schema in self.schema_loading:
			self.set_autocommit_db(False)
			schema_loading = self.schema_loading[schema]["loading"]
			schema_destination = self.schema_loading[schema]["destination"]
			schema_temporary = "_rename_%s" % self.schema_loading[schema]["destination"]
			sql_dest_to_tmp = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(schema_destination), sql.Identifier(schema_temporary))
			sql_load_to_dest = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(schema_loading), sql.Identifier(schema_destination))
			sql_tmp_to_load = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(schema_temporary), sql.Identifier(schema_loading))
			self.logger.info("Swapping schema %s with %s" % (schema_destination, schema_loading))
			self.logger.debug("Renaming schema %s in %s" % (schema_destination, schema_temporary))
			self.pgsql_cur.execute(sql_dest_to_tmp)
			self.logger.debug("Renaming schema %s in %s" % (schema_loading, schema_destination))
			self.pgsql_cur.execute(sql_load_to_dest)
			self.logger.debug("Renaming schema %s in %s" % (schema_temporary, schema_loading))
			self.pgsql_cur.execute(sql_tmp_to_load)
			
			schema_loading = self.schema_loading[schema]["loading_obfuscated"]
			schema_destination = self.schema_loading[schema]["obfuscated"]
			schema_temporary = "_rename_%s" % self.schema_loading[schema]["obfuscated"]
			sql_dest_to_tmp = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(schema_destination), sql.Identifier(schema_temporary))
			sql_load_to_dest = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(schema_loading), sql.Identifier(schema_destination))
			sql_tmp_to_load = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(schema_temporary), sql.Identifier(schema_loading))
			self.logger.info("Swapping schema %s with %s" % (schema_destination, schema_loading))
			self.logger.debug("Renaming schema %s in %s" % (schema_destination, schema_temporary))
			self.pgsql_cur.execute(sql_dest_to_tmp)
			self.logger.debug("Renaming schema %s in %s" % (schema_loading, schema_destination))
			self.pgsql_cur.execute(sql_load_to_dest)
			self.logger.debug("Renaming schema %s in %s" % (schema_temporary, schema_loading))
			self.pgsql_cur.execute(sql_tmp_to_load)
			
			self.logger.debug("Commit the swap transaction" )
			self.pgsql_conn.commit()
			self.set_autocommit_db(True)
	
	def set_batch_processed(self, id_batch):
		"""
			The method updates the flag b_processed and sets the processed timestamp for the given batch id.
			The event ids are aggregated into the table t_batch_events used by the replay function.
			
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
		self.pgsql_cur.execute(sql_update, (id_batch, ))
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
		self.pgsql_cur.execute(sql_collect_events, (id_batch, ))
	
	def swap_tables(self):
		"""
			The method loops over the tables stored in the class 
		"""
		self.set_autocommit_db(False)
		for schema in self.schema_tables:
			schema_loading = self.schema_loading[schema]["loading"]
			schema_destination = self.schema_loading[schema]["destination"]
			schema_loading_obfuscated = self.schema_loading[schema]["loading_obfuscated"]
			schema_obfuscated = self.schema_loading[schema]["obfuscated"]
			try:
				obfuscated_tables = [table for table in self.obfuscation[schema]]
				clear_tables = [table for table in self.schema_tables[schema] if table not in self.obfuscation[schema]]
			except:
				obfuscated_tables = []
				clear_tables = [table for table in self.schema_tables[schema] ]
			
			for table in self.schema_tables[schema]:
				self.logger.info("Swapping table %s.%s with %s.%s" % (schema_destination, table, schema_loading, table))
				sql_drop_origin = sql.SQL("DROP TABLE IF EXISTS {}.{} CASCADE;").format(sql.Identifier(schema_destination),sql.Identifier(table))
				sql_set_schema_new = sql.SQL("ALTER TABLE {}.{} SET SCHEMA {};").format(sql.Identifier(schema_loading),sql.Identifier(table), sql.Identifier(schema_destination))
				self.logger.debug("Dropping the original table %s.%s " % (schema_destination, table))
				self.pgsql_cur.execute(sql_drop_origin)
				self.logger.debug("Changing the schema for table %s.%s to %s" % (schema_loading, table, schema_destination))
				self.pgsql_cur.execute(sql_set_schema_new)
				self.pgsql_conn.commit()
			self.set_autocommit_db(True)	
			for table in self.schema_tables[schema]:
				self.logger.info("Swapping obfuscated relation %s.%s with %s.%s" % (schema_obfuscated, table, schema_loading_obfuscated, table))
				sql_drop_view = sql.SQL("DROP VIEW IF EXISTS {}.{} CASCADE;").format(sql.Identifier(schema_obfuscated),sql.Identifier(table))
				sql_drop_table = sql.SQL("DROP TABLE IF EXISTS {}.{} CASCADE;").format(sql.Identifier(schema_obfuscated),sql.Identifier(table))
				try:
					self.pgsql_cur.execute(sql_drop_view)
				except:
					self.pgsql_cur.execute(sql_drop_table)
			for table in obfuscated_tables:
				sql_set_schema_table = sql.SQL("ALTER TABLE {}.{} SET SCHEMA {};").format(sql.Identifier(schema_loading_obfuscated),sql.Identifier(table), sql.Identifier(schema_obfuscated))
				try:
					self.pgsql_cur.execute(sql_set_schema_table)
				except:
					pass
			for table in clear_tables:
				sql_set_schema_view = sql.SQL("ALTER VIEW {}.{} SET SCHEMA {};").format(sql.Identifier(schema_loading_obfuscated),sql.Identifier(table), sql.Identifier(schema_obfuscated))
				try:
					self.pgsql_cur.execute(sql_set_schema_view)
				except:
					pass
				
		
	
	def create_database_schema(self, schema_name):
		"""
			The method creates a database schema.
			The create schema is issued with the clause IF NOT EXISTS.
			Should the schema be already present the create is skipped.
			
			:param schema_name: The schema name to be created. 
		"""
		sql_create = sql.SQL("CREATE SCHEMA IF NOT EXISTS {};").format(sql.Identifier(schema_name))
		self.pgsql_cur.execute(sql_create)

	def upgrade_catalogue_v1(self):
		"""
			The method upgrade a replica catalogue  from version 1 to version 2.
			The original catalogue is not altered but just renamed.
			All the existing data are transferred into the new catalogue loaded  using the create_schema.sql file.
		"""
		replay_max_rows = 10000
		self.__v2_schema = "_sch_ninja_version2"
		self.__current_schema = "sch_ninja"
		self.__v1_schema = "_sch_ninja_version1"
		self.connect_db()
		upgrade_possible = True
		
		sql_get_min_max = """
			SELECT 
				sch_ninja.binlog_max(
					ARRAY[
						t_binlog_name,
						i_binlog_position::text
					]
				),
				sch_ninja.binlog_min(
					ARRAY[
						t_binlog_name,
						i_binlog_position::text
					]
				)
			FROM 
				sch_ninja.t_replica_tables
			WHERE
				i_id_source=%s
			;

		"""
		
		sql_migrate_tables = """
			WITH t_old_new AS
				(
					SELECT 
						old.i_id_source as id_source_old,
						new.i_id_source as id_source_new,
						ARRAY[new.t_dest_schema,new.t_obf_schema] AS t_dest_schema
					FROM 
						_sch_ninja_version1.t_sources  old
						INNER JOIN (
							
								SELECT 
									i_id_source,
									t_sch_map->>'obfuscate' as t_obf_schema,
									t_sch_map->>'clear' as t_dest_schema
									
								FROM
								(
									SELECT 
										i_id_source,
										(jsonb_each_text(jsb_schema_mappings)).value::json as t_sch_map

									FROM 
										sch_ninja.t_sources
								) sch

							   ) new 
						ON 	
								old.t_dest_schema=new.t_dest_schema
							AND	old.t_obf_schema=new.t_obf_schema
				)
				INSERT INTO sch_ninja.t_replica_tables
					(
						i_id_source,
						v_table_name,
						v_schema_name,
						v_table_pkey,
						t_binlog_name,
						i_binlog_position,
						b_replica_enabled
					)

				SELECT distinct
					id_source_new,
					v_table_name,
					unnest(t_dest_schema),
					string_to_array(replace(v_table_pkey[1],'"',''),',') as table_pkey,
					bat.t_binlog_name,
					bat.i_binlog_position,
					't'::boolean as b_replica_enabled
					
				FROM 
					_sch_ninja_version1.t_replica_batch bat
					INNER JOIN _sch_ninja_version1.t_replica_tables tab
					ON tab.i_id_source=bat.i_id_source
					
					INNER JOIN t_old_new
					ON tab.i_id_source=t_old_new.id_source_old
				WHERE
						NOT bat.b_processed
					AND  bat.b_started
				ORDER BY v_table_name
			
					;


		"""
		
		sql_mapping = """
			WITH t_mapping AS
				(
					SELECT 
						t_sch_map->>'obfuscate' as t_obf_schema,
						t_sch_map->>'clear' as t_dest_schema
						
					FROM
					(
						SELECT (json_each_text(%s::json)).value::json AS t_sch_map
					) sch
				)

			SELECT 
				mapped_schema=config_schema as match_mapping,
				mapped_list,
				config_list
			FROM
			(
				SELECT 
					count(dst.t_dest_schema) as mapped_schema,
					string_agg(dst.t_dest_schema,' ') as mapped_list
				FROM
					t_mapping dst 
					INNER JOIN sch_ninja.t_sources src
					ON 
							src.t_dest_schema=dst.t_dest_schema
						AND	src.t_obf_schema= dst.t_obf_schema
			) cnt_map,
			(
				SELECT 
					count(t_dest_schema) as config_schema,
					string_agg(t_dest_schema,' ') as config_list
				FROM
					t_mapping 

			) cnt_cnf
			;

		"""
		
		self.logger.info("Checking if we need to replay data in the existing catalogue")
		sql_check = """
			SELECT 
				src.i_id_source,
				src.t_source,
				count(log.i_id_event)
			FROM 
				sch_ninja.t_log_replica log 
				INNER JOIN sch_ninja.t_replica_batch bat 
					ON log.i_id_batch=bat.i_id_batch
				INNER JOIN sch_ninja.t_sources src
					ON src.i_id_source=bat.i_id_source
			GROUP BY
				src.i_id_source,
				src.t_source
			;

		"""
		self.pgsql_cur.execute(sql_check)	
		source_replay = self.pgsql_cur.fetchall()	
		if source_replay:
			for source in source_replay:
				id_source = source[0]
				source_name = source[1]
				replay_rows = source[2]
				self.logger.info("Replaying last %s rows for source %s " % (replay_rows, source_name))
				continue_loop = True
				while continue_loop:
					sql_replay = """SELECT sch_ninja.fn_process_batch(%s,%s);"""
					self.pgsql_cur.execute(sql_replay, (replay_max_rows, id_source, ))
					replay_status = self.pgsql_cur.fetchone()
					continue_loop = replay_status[0]
					if continue_loop:
						self.logger.info("Still replaying rows for source %s" % ( source_name, ) )
		self.logger.info("Checking if the schema mappings are correctly matched")
		for source in self.sources:
			schema_mappings = json.dumps(self.sources[source]["schema_mappings"])
			self.pgsql_cur.execute(sql_mapping, (schema_mappings, ))
			config_mapping = self.pgsql_cur.fetchone()
			print(config_mapping)
			source_mapped = config_mapping[0]
			list_mapped = config_mapping[1]
			list_config = config_mapping[2]
			if not source_mapped:
				self.logger.error("Checks for source %s failed. Matched mappings %s, configured mappings %s" % (source, list_mapped, list_config))
				upgrade_possible = False
		if upgrade_possible:	
			try:
				self.logger.info("Renaming the old schema %s in %s " % (self.__v2_schema, self.__v1_schema))
				sql_rename_old = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(self.__current_schema), sql.Identifier(self.__v1_schema))
				self.pgsql_cur.execute(sql_rename_old)
				self.logger.info("Installing the new replica catalogue " )	
				self.create_replica_schema()
				for source in self.sources:
					self.source = source
					self.add_source()
					
				self.pgsql_cur.execute(sql_migrate_tables)
				for source in self.sources:
					self.source = source
					self.set_source_id()
					self.pgsql_cur.execute(sql_get_min_max, (self.i_id_source, ))
					min_max = self.pgsql_cur.fetchone() 
					max_position = min_max[0]
					min_position = min_max[1]
					
					master_data = {}
					master_status = []
					master_data["File"] = min_position[0]
					master_data["Position"] = min_position[1]
					master_status.append(master_data)
					self.save_master_status(master_status)
					
					master_status = []
					master_data["File"] = max_position[0]
					master_data["Position"] = max_position[1]
					master_status.append(master_data)
					self.set_source_highwatermark(master_status, False)
					
			except:
				self.rollback_upgrade_v1()
				raise
		else: 
			self.logger.error("Sanity checks for the schema mappings failed. Aborting the upgrade")
			self.rollback_upgrade_v1()
		self.disconnect_db()
		
	def rollback_upgrade_v1(self):
		"""
			The procedure rollsback the upgrade dropping the schema sch_ninja and renaming the version 1 to the 
		"""
		sql_check="""
			SELECT 
				count(*)
			FROM 
				information_schema.schemata  
			WHERE 
				schema_name=%s
		"""
		self.pgsql_cur.execute(sql_check, (self.__v1_schema, ))
		v1_schema = self.pgsql_cur.fetchone()
		if v1_schema[0] == 1:
			self.logger.info("The schema %s exists, rolling back the changes" % (self.__v1_schema))
			self.pgsql_cur.execute(sql_check, (self.__current_schema, ))
			curr_schema = self.pgsql_cur.fetchone()
			if curr_schema[0] == 1:
				self.logger.info("Renaming the current schema %s in %s" % (self.__current_schema, self.__v2_schema))
				sql_rename_current = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(self.__current_schema), sql.Identifier(self.__v2_schema))
				self.pgsql_cur.execute(sql_rename_current)
			sql_rename_old = sql.SQL("ALTER SCHEMA {} RENAME TO {};").format(sql.Identifier(self.__v1_schema), sql.Identifier(self.__current_schema))
			self.pgsql_cur.execute(sql_rename_old)
		else:
			self.logger.info("The old schema %s does not exists, aborting the rollback" % (self.__v1_schema))
			sys.exit()
		self.logger.info("Rollback successful. Please note the catalogue version 2 has been renamed to %s for debugging.\nYou will need to drop it before running another upgrade" % (self.__v2_schema, ))


	def drop_database_schema(self, schema_name, cascade):
		"""
			The method drops a database schema.
			The drop can be either schema is issued with the clause IF NOT EXISTS.
			Should the schema be already present the create is skipped.
			
			:param schema_name: The schema name to be created. 
			:param schema_name: If true the schema is dropped with the clause cascade. 
		"""
		if cascade:
			cascade_clause = "CASCADE"
		else:
			cascade_clause = ""
		sql_drop = "DROP SCHEMA IF EXISTS {} %s;" % cascade_clause
		sql_drop = sql.SQL(sql_drop).format(sql.Identifier(schema_name))
		self.set_lock_timeout()
		try:
			self.pgsql_cur.execute(sql_drop)
		except:
			self.logger.error("could not drop the schema %s. You will need to drop it manually." % schema_name)
