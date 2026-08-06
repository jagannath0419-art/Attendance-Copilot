import os
import math
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_mysqldb import MySQL
import MySQLdb.cursors
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
# Secure session key setting
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "attendance_copilot_secret_session_key_99")

# MySQL Database Configuration
app.config['MYSQL_HOST'] = os.environ.get("DB_HOST", "localhost")
app.config['MYSQL_USER'] = os.environ.get("DB_USER", "root")
app.config['MYSQL_PASSWORD'] = os.environ.get("DB_PASS", "")
app.config['MYSQL_DB'] = os.environ.get("DB_NAME", "attendance_copilot")
app.config['MYSQL_PORT'] = int(os.environ.get("DB_PORT", 3306))

mysql = MySQL(app)

def init_db_schema():
    """Silently ensures that all database tables are ready for use during startup."""
    cursor = mysql.connection.cursor()
    try:
        # 1. Create Users Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                fullname VARCHAR(255) NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                department VARCHAR(100),
                semester VARCHAR(50)
            ) ENGINE=InnoDB;
        """)
        # 2. Create Subjects Table with Cascade Delete Constraints
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subjects (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                subject_name VARCHAR(255) NOT NULL,
                structure_type VARCHAR(50) NOT NULL,
                active_month VARCHAR(50),
                start_date DATE,
                end_date DATE,
                color_theme VARCHAR(50),
                attendance_target INT DEFAULT 75,
                theory_conducted INT DEFAULT 0,
                theory_attended INT DEFAULT 0,
                labs_conducted INT DEFAULT 0,
                labs_attended INT DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            ) ENGINE=InnoDB;
        """)
        # 3. Create Session Logs History Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS attendance_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                subject_id INT NOT NULL,
                session_type ENUM('Theory', 'Practical') NOT NULL,
                status ENUM('Attended', 'Missed') NOT NULL,
                lecture_date DATE NOT NULL,
                time_slot VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            ) ENGINE=InnoDB;
        """)
        mysql.connection.commit()
    except Exception as e:
        print(f"Error initializing DB schema: {e}")
    finally:
        cursor.close()

# Execute schema validation at boot
with app.app_context():
    try:
        init_db_schema()
    except Exception as e:
        print(f"Database validation connection failed: {e}")

def calculate_subject_analytics(sub, target_goal):
    """Calculates attendance percentages and predicts safe skip margins or safe-zone streak margins."""
    tc = sub.get('theory_conducted') or 0
    ta = sub.get('theory_attended') or 0
    lc = sub.get('labs_conducted') or 0
    la = sub.get('labs_attended') or 0
    st = sub.get('structure_type')

    total_conducted = 0
    total_attended = 0
    
    if st == "Theory Only":
        total_conducted, total_attended = tc, ta
    elif st == "Practical Only":
        total_conducted, total_attended = lc, la
    else:
        total_conducted, total_attended = (tc + lc), (ta + la)

    current_pct = 100 if total_conducted == 0 else round((total_attended / total_conducted) * 100)
    target_fraction = target_goal / 100.0
    
    if current_pct >= target_goal:
        status = "Safe"
        # Calculate how many next lectures can be missed without breaking target boundaries
        skippable = math.floor((total_attended - (total_conducted * target_fraction)) / target_fraction) if target_fraction > 0 else 0
        skippable = max(0, skippable)
        guidance_msg = f"Safe! You can skip the next <strong>{skippable}</strong> session(s)."
    else:
        status = "Defaulter"
        # Calculate streak needed to crawl back up to target
        required_streak = math.ceil(((target_fraction * total_conducted) - total_attended) / (1.0 - target_fraction)) if target_fraction < 1 else 1
        guidance_msg = f"Defaulter! You must attend the next <strong>{max(1, required_streak)}</strong> session(s) consecutively."

    return current_pct, total_conducted, total_attended, status, guidance_msg

@app.route('/')
def home():
    processed_subjects = []
    # Initialize global fallbacks
    global_metrics = {"avg": 0, "total_cond": 0, "total_att": 0, "defaulters": 0, "safe": 0}
    
    if 'user_id' in session:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT * FROM subjects WHERE user_id = %s", (session['user_id'],))
        raw_subjects = cursor.fetchall()
        
        total_cond, total_att, def_count, safe_count = 0, 0, 0, 0
        
        for s in raw_subjects:
            pct, cond, att, status, msg = calculate_subject_analytics(s, s['attendance_target'])
            s.update({
                'calculated_percentage': pct, 
                'calculated_status': status, 
                'calculated_message': msg,
                'start_date': str(s['start_date']) if s['start_date'] else '',
                'end_date': str(s['end_date']) if s['end_date'] else ''
            })
            total_cond += cond
            total_att += att
            if status == "Defaulter": 
                def_count += 1
            else: 
                safe_count += 1
            processed_subjects.append(s)
        
        global_metrics = {
            "avg": round((total_att / total_cond) * 100) if total_cond > 0 else 100,
            "total_cond": total_cond, 
            "total_att": total_att, 
            "defaulters": def_count, 
            "safe": safe_count
        }
        cursor.close()
    
    return render_template('index.html', subjects=processed_subjects, metrics=global_metrics)

@app.route('/auth/register', methods=['POST'])
def register():
    fullname = request.form.get('fullname')
    email = request.form.get('email')
    password = generate_password_hash(request.form.get('password'))
    department = request.form.get('department')
    semester = request.form.get('semester')
    
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute(
            "INSERT INTO users (fullname, email, password, department, semester) VALUES (%s, %s, %s, %s, %s)", 
            (fullname, email, password, department, semester)
        )
        mysql.connection.commit()
        
        cursor.execute("SELECT id, fullname, department, semester FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        if user:
            session['user_id'] = user['id']
            session['fullname'] = user['fullname']
            session['department'] = user['department']
            session['semester'] = user['semester']
    except Exception as e:
        print(f"Registration Error: {e}")
    finally:
        cursor.close()
    return redirect(url_for('home'))

@app.route('/auth/login', methods=['POST'])
def login():
    email = request.form.get('email')
    password = request.form.get('password')
    
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        if user and check_password_hash(user['password'], password):
            session.update({
                'user_id': user['id'], 
                'fullname': user['fullname'], 
                'department': user['department'], 
                'semester': user['semester']
            })
    except Exception as e:
        print(f"Login error: {e}")
    finally:
        cursor.close()
    return redirect(url_for('home'))

@app.route('/auth/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/edit_profile', methods=['POST'])
def edit_profile():
    if 'user_id' not in session:
        return redirect(url_for('home'))
        
    fullname = request.form.get('fullname')
    department = request.form.get('department')
    semester = request.form.get('semester')
    
    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            "UPDATE users SET fullname = %s, department = %s, semester = %s WHERE id = %s",
            (fullname, department, semester, session['user_id'])
        )
        mysql.connection.commit()
        session['fullname'] = fullname
        session['department'] = department
        session['semester'] = semester
    except Exception as e:
        print(f"Error updating profile: {e}")
    finally:
        cursor.close()
    return redirect(url_for('home'))

@app.route('/add_subject', methods=['POST'])
def add_subject():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    subject_name = request.form.get('subject_name')
    structure_type = request.form.get('structure_type')
    active_month = request.form.get('active_month')
    color_theme = request.form.get('color_theme')
    start_date = request.form.get('start_date') or None
    end_date = request.form.get('end_date') or None
    
    attendance_target = int(request.form.get('attendance_target') or 75)
    theory_conducted = int(request.form.get('theory_conducted') or 0)
    theory_attended = int(request.form.get('theory_attended') or 0)
    labs_conducted = int(request.form.get('labs_conducted') or 0)
    labs_attended = int(request.form.get('labs_attended') or 0)

    cursor = mysql.connection.cursor()
    try:
        query = """
            INSERT INTO subjects (
                user_id, subject_name, structure_type, active_month, 
                start_date, end_date, color_theme, attendance_target, 
                theory_conducted, theory_attended, labs_conducted, labs_attended
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        values = (
            session['user_id'], subject_name, structure_type, active_month,
            start_date, end_date, color_theme, attendance_target,
            theory_conducted, theory_attended, labs_conducted, labs_attended
        )
        cursor.execute(query, values)
        mysql.connection.commit()
    except Exception as e:
        print(f"Error adding subject: {e}")
    finally:
        cursor.close()

    return redirect(url_for('home'))

@app.route('/edit_subject', methods=['POST'])
def edit_subject():
    if 'user_id' not in session:
        return redirect(url_for('home'))
        
    subject_id = request.form.get('subject_id')
    subject_name = request.form.get('subject_name')
    structure_type = request.form.get('structure_type')
    active_month = request.form.get('active_month')
    color_theme = request.form.get('color_theme')
    start_date = request.form.get('start_date') or None
    end_date = request.form.get('end_date') or None
    
    attendance_target = int(request.form.get('attendance_target') or 75)
    theory_conducted = int(request.form.get('theory_conducted') or 0)
    theory_attended = int(request.form.get('theory_attended') or 0)
    labs_conducted = int(request.form.get('labs_conducted') or 0)
    labs_attended = int(request.form.get('labs_attended') or 0)

    cursor = mysql.connection.cursor()
    try:
        query = """
            UPDATE subjects SET 
                subject_name = %s, structure_type = %s, active_month = %s, 
                color_theme = %s, start_date = %s, end_date = %s, attendance_target = %s,
                theory_conducted = %s, theory_attended = %s, labs_conducted = %s, labs_attended = %s
            WHERE id = %s AND user_id = %s
        """
        values = (
            subject_name, structure_type, active_month, color_theme, start_date, end_date, 
            attendance_target, theory_conducted, theory_attended, labs_conducted, labs_attended,
            subject_id, session['user_id']
        )
        cursor.execute(query, values)
        mysql.connection.commit()
    except Exception as e:
        print(f"Error editing subject: {e}")
    finally:
        cursor.close()
            
    return redirect(url_for('home'))

@app.route('/delete_subject', methods=['POST'])
def delete_subject():
    if 'user_id' not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
        
    subject_id = request.form.get('subject_id')
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("DELETE FROM subjects WHERE id = %s AND user_id = %s", (subject_id, session['user_id']))
        mysql.connection.commit()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Error deleting subject: {e}")
        return jsonify({"success": False, "error": str(e)})
    finally:
        cursor.close()

@app.route('/update_counter', methods=['POST'])
def update_counter():
    """Updates individual lecture counters and logs time parameters securely to history table."""
    if 'user_id' not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
        
    data = request.get_json()
    subject_id = data.get('subject_id')
    field = data.get('field') # theory_conducted, theory_attended, labs_conducted, labs_attended
    action = data.get('action') # increment / decrement
    lecture_date = data.get('lecture_date')
    time_slot = data.get('time_slot')
    
    if field not in ['theory_conducted', 'theory_attended', 'labs_conducted', 'labs_attended']:
        return jsonify({"success": False, "error": "Invalid field"}), 400
        
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        # Retrieve current field state
        cursor.execute("SELECT * FROM subjects WHERE id = %s AND user_id = %s", (subject_id, session['user_id']))
        subject = cursor.fetchone()
        
        if not subject:
            return jsonify({"success": False, "error": "Subject not found"}), 404
            
        current_val = subject.get(field) or 0
        
        # Apply increment/decrement math boundaries safely
        if action == 'increment':
            new_val = current_val + 1
        elif action == 'decrement':
            new_val = max(0, current_val - 1)
        else:
            return jsonify({"success": False, "error": "Invalid action"}), 400
            
        # Field validation mapping
        temp_subject = subject.copy()
        temp_subject[field] = new_val
        
        # If incrementing an attended class, also auto-increment conducted to preserve mathematical domain (Attended <= Conducted)
        if action == 'increment' and 'attended' in field:
            conducted_partner = field.replace('attended', 'conducted')
            temp_subject[conducted_partner] = (temp_subject.get(conducted_partner) or 0) + 1
            
        tc = temp_subject.get('theory_conducted') or 0
        ta = temp_subject.get('theory_attended') or 0
        lc = temp_subject.get('labs_conducted') or 0
        la = temp_subject.get('labs_attended') or 0
        
        if ta > tc or la > lc:
            return jsonify({"success": False, "error": "Attended lectures cannot exceed conducted lectures."}), 400
            
        # 1. Update counter fields on parent table
        if action == 'increment' and 'attended' in field:
            conducted_partner = field.replace('attended', 'conducted')
            cursor.execute(f"UPDATE subjects SET {field} = %s, {conducted_partner} = {conducted_partner} + 1 WHERE id = %s AND user_id = %s", (new_val, subject_id, session['user_id']))
        else:
            cursor.execute(f"UPDATE subjects SET {field} = %s WHERE id = %s AND user_id = %s", (new_val, subject_id, session['user_id']))
            
        # 2. Log History Record if increment action occurred with date/time parameters
        if action == 'increment' and lecture_date and time_slot:
            session_type = 'Theory' if 'theory' in field else 'Practical'
            status = 'Attended' if 'attended' in field else 'Missed'
            
            cursor.execute("""
                INSERT INTO attendance_logs (subject_id, session_type, status, lecture_date, time_slot)
                VALUES (%s, %s, %s, %s, %s)
            """, (subject_id, session_type, status, lecture_date, time_slot))
            
        mysql.connection.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cursor.close()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)